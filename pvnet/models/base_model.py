"""Base model for all PVNet submodels"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Union

import hydra
import lightning.pytorch as pl
import pandas as pd
import torch
import torch.nn.functional as F
import wandb
from huggingface_hub import ModelCard, ModelCardData, PyTorchModelHubMixin
from huggingface_hub.constants import CONFIG_NAME, PYTORCH_WEIGHTS_NAME
from huggingface_hub.file_download import hf_hub_download
from huggingface_hub.utils._deprecation import _deprecate_positional_args
from nowcasting_utils.models.loss import WeightedLosses
from nowcasting_utils.models.metrics import (
    mae_each_forecast_horizon,
    mse_each_forecast_horizon,
)
from ocf_datapipes.utils.consts import BatchKey
from ocf_ml_metrics.evaluation.evaluation import evaluation
from pytorch_forecasting.metrics import QuantileLoss

from pvnet.models.utils import (
    BatchAccumulator,
    MetricAccumulator,
    PredAccumulator,
)
from pvnet.optimizers import AbstractOptimizer
from pvnet.utils import construct_ocf_ml_metrics_batch_df, plot_batch_forecasts

logger = logging.getLogger(__name__)

activities = [torch.profiler.ProfilerActivity.CPU]
if torch.cuda.is_available():
    activities.append(torch.profiler.ProfilerActivity.CUDA)


class PVNetModelHubMixin(PyTorchModelHubMixin):
    """
    Implementation of [`PyTorchModelHubMixin`] to provide model Hub upload/download capabilities.
    """

    @classmethod
    @_deprecate_positional_args(version="0.16")
    def _from_pretrained(
        cls,
        *,
        model_id: str,
        revision: str,
        cache_dir: str,
        force_download: bool,
        proxies: Optional[Dict],
        resume_download: bool,
        local_files_only: bool,
        token: Union[str, bool, None],
        map_location: str = "cpu",
        strict: bool = False,
        **model_kwargs,
    ):
        """Load Pytorch pretrained weights and return the loaded model."""
        if os.path.isdir(model_id):
            print("Loading weights from local directory")
            model_file = os.path.join(model_id, PYTORCH_WEIGHTS_NAME)
        else:
            model_file = hf_hub_download(
                repo_id=model_id,
                filename=PYTORCH_WEIGHTS_NAME,
                revision=revision,
                cache_dir=cache_dir,
                force_download=force_download,
                proxies=proxies,
                resume_download=resume_download,
                token=token,
                local_files_only=local_files_only,
            )

        if "config" not in model_kwargs:
            raise ValueError("Config must be supplied to instantiate model")

        model_kwargs.update(model_kwargs.pop("config"))
        model = hydra.utils.instantiate(model_kwargs)

        state_dict = torch.load(model_file, map_location=torch.device(map_location))
        model.load_state_dict(state_dict, strict=strict)  # type: ignore
        model.eval()  # type: ignore

        return model

    @_deprecate_positional_args(version="0.16")
    def save_pretrained(
        self,
        save_directory: Union[str, Path],
        config: dict,
        repo_id: Optional[str] = None,
        push_to_hub: bool = False,
        wandb_model_code: Optional[str] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        Save weights in local directory.

        Args:
            save_directory (`str` or `Path`):
                Path to directory in which the model weights and configuration will be saved.
            config (`dict`):
                Model configuration specified as a key/value dictionary.
            repo_id (`str`, *optional*):
                ID of your repository on the Hub. Used only if `push_to_hub=True`. Will default to
                the folder name if not provided.
            push_to_hub (`bool`, *optional*, defaults to `False`):
                Whether or not to push your model to the Huggingface Hub after saving it.
            wandb_model_code: Identifier of the model on wandb.
            kwargs:
                Additional key word arguments passed along to the
                [`~ModelHubMixin._from_pretrained`] method.
        """

        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)

        # saving model weights/files
        self._save_pretrained(save_directory)

        # saving config
        if isinstance(config, dict):
            (save_directory / CONFIG_NAME).write_text(json.dumps(config))

        # Creating and saving model card.
        card_data = ModelCardData(language="en", license="mit", library_name="pytorch")
        card_template_path = f"{os.path.dirname(os.path.abspath(__file__))}/model_card_template.md"

        card = ModelCard.from_template(
            card_data,
            template_path=card_template_path,
            wandb_model_code=wandb_model_code,
        )

        (save_directory / "README.md").write_text(str(card))

        if push_to_hub:
            kwargs = kwargs.copy()  # soft-copy to avoid mutating input
            if config is not None:  # kwarg for `push_to_hub`
                kwargs["config"] = config
            if repo_id is None:
                repo_id = save_directory.name  # Defaults to `save_directory` name
            return self.push_to_hub(repo_id=repo_id, **kwargs)
        return None


class BaseModel(pl.LightningModule, PVNetModelHubMixin):
    """Abtstract base class for PVNet submodels"""

    def __init__(
        self,
        history_minutes: int,
        forecast_minutes: int,
        optimizer: AbstractOptimizer,
        output_quantiles: Optional[list[float]] = None,
    ):
        """Abtstract base class for PVNet submodels.

        Args:
            history_minutes (int): Length of the GSP history period in minutes
            forecast_minutes (int): Length of the GSP forecast period in minutes
            optimizer (AbstractOptimizer): Optimizer
            output_quantiles: A list of float (0.0, 1.0) quantiles to predict values for. If set to
                None the output is a single value.
        """
        super().__init__()

        self._optimizer = optimizer

        # Model must have lr to allow tuning
        # This setting is only used when lr is tuned with callback
        self.lr = None

        self.history_minutes = history_minutes
        self.forecast_minutes = forecast_minutes
        self.output_quantiles = output_quantiles
        if output_quantiles is not None:
            self._quantile_loss = QuantileLoss(quantiles=output_quantiles)

        # Number of timestemps for 30 minutely data
        self.history_len_30 = history_minutes // 30
        self.forecast_len_30 = forecast_minutes // 30

        self.weighted_losses = WeightedLosses(forecast_length=self.forecast_len_30)

        self._accumulated_metrics = MetricAccumulator()
        self._accumulated_batches = BatchAccumulator()
        self._accumulated_y_hat = PredAccumulator()

    def _quantiles_to_prediction(self, y_quantiles):
        # y_quantiles Shape: batch_size, seq_length, num_quantiles
        return self._quantile_loss.to_prediction(y_quantiles)

    def _calculate_qauntile_loss(self, y_quantiles, y):
        return self._quantile_loss.loss(y_quantiles, y).mean()

    def _calculate_common_losses(self, y, y_hat):
        """Calculate losses common to train, test, and val"""

        losses = {}

        if self.output_quantiles is not None:
            losses["quantile_loss"] = self._calculate_qauntile_loss(y_hat, y)
            y_hat = self._quantiles_to_prediction(y_hat)

        # calculate mse, mae
        mse_loss = F.mse_loss(y_hat, y)
        mae_loss = F.l1_loss(y_hat, y)

        # calculate mse, mae with exp weighted loss
        mse_exp = self.weighted_losses.get_mse_exp(output=y_hat, target=y)
        mae_exp = self.weighted_losses.get_mae_exp(output=y_hat, target=y)

        # TODO: Compute correlation coef using np.corrcoef(tensor with
        # shape (2, num_timesteps))[0, 1] on each example, and taking
        # the mean across the batch?
        losses.update(
            {
                "MSE": mse_loss,
                "MAE": mae_loss,
                "MSE_EXP": mse_exp,
                "MAE_EXP": mae_exp,
            }
        )

        return losses

    def _calculate_val_losses(self, y, y_hat):
        """Calculate additional validation losses"""
        if self.output_quantiles is not None:
            y_hat = self._quantiles_to_prediction(y_hat)

        mse_each_step = mse_each_forecast_horizon(output=y_hat, target=y)
        mae_each_step = mae_each_forecast_horizon(output=y_hat, target=y)

        losses = {f"MSE_horizon/step_{i:02}": m for i, m in enumerate(mse_each_step)}
        losses.update({f"MAE_horizon/step_{i:02}": m for i, m in enumerate(mae_each_step)})
        return losses

    def _calculate_test_losses(self, y, y_hat):
        """Calculate additional test losses"""
        # No additional test losses
        losses = {}
        return losses

    def _training_accumulate_log(self, batch, batch_idx, losses, y_hat):
        """Internal function to accumulate training batches and log results.

        This is used when accummulating grad batches. Should make the variability in logged training
        step metrics indpendent on whether we accumulate N batches of size B or just use a larger
        batch size of N*B with no accumulaion.
        """

        losses = {k: v.detach().cpu() for k, v in losses.items()}
        y_hat = y_hat.detach().cpu()

        self._accumulated_metrics.append(losses)
        self._accumulated_batches.append(batch)
        self._accumulated_y_hat.append(y_hat)

        if not self.trainer.fit_loop._should_accumulate():
            losses = self._accumulated_metrics.flush()
            batch = self._accumulated_batches.flush()
            y_hat = self._accumulated_y_hat.flush()

            self.log_dict(
                losses,
                on_step=True,
                on_epoch=True,
            )

            # Number of accumulated grad batches
            grad_batch_num = (batch_idx + 1) / self.trainer.accumulate_grad_batches

            # We only create the figure every 8 log steps
            # This was reduced as it was creating figures too often
            if grad_batch_num % (8 * self.trainer.log_every_n_steps) == 0:
                fig = plot_batch_forecasts(batch, y_hat, batch_idx, quantiles=self.output_quantiles)
                fig.savefig("latest_logged_train_batch.png")

    def training_step(self, batch, batch_idx):
        """Run training step"""
        y_hat = self(batch)
        y = batch[BatchKey.gsp][:, -self.forecast_len_30 :, 0]

        losses = self._calculate_common_losses(y, y_hat)
        losses = {f"{k}/train": v for k, v in losses.items()}

        self._training_accumulate_log(batch, batch_idx, losses, y_hat)

        if self.output_quantiles is not None:
            opt_target = losses["quantile_loss/train"]
        else:
            opt_target = losses["MAE/train"]
        return opt_target

    def validation_step(self, batch: dict, batch_idx):
        """Run validation step"""
        y_hat = self(batch)
        y = batch[BatchKey.gsp][:, -self.forecast_len_30 :, 0]

        losses = self._calculate_common_losses(y, y_hat)
        losses.update(self._calculate_val_losses(y, y_hat))

        logged_losses = {f"{k}/val": v for k, v in losses.items()}

        self.log_dict(
            logged_losses,
            on_step=False,
            on_epoch=True,
        )

        accum_batch_num = batch_idx // self.trainer.accumulate_grad_batches

        if accum_batch_num in [0, 1]:
            # Store these temporarily under self
            if not hasattr(self, "_val_y_hats"):
                self._val_y_hats = PredAccumulator()
                self._val_batches = BatchAccumulator()

            self._val_y_hats.append(y_hat)
            self._val_batches.append(batch)

            # if batch had accumulated
            if (batch_idx + 1) % self.trainer.accumulate_grad_batches == 0:
                y_hat = self._val_y_hats.flush()
                batch = self._val_batches.flush()

                fig = plot_batch_forecasts(batch, y_hat, quantiles=self.output_quantiles)

                self.logger.experiment.log(
                    {
                        f"val_forecast_samples/batch_idx_{accum_batch_num}": wandb.Image(fig),
                    }
                )
                del self._val_y_hats
                del self._val_batches

        return logged_losses

    def test_step(self, batch, batch_idx):
        """Run test step"""
        y_hat = self(batch)
        y = batch[BatchKey.gsp][:, -self.forecast_len_30 :, 0]

        losses = self._calculate_common_losses(y, y_hat)
        losses.update(self._calculate_val_losses(y, y_hat))
        losses.update(self._calculate_test_losses(y, y_hat))
        logged_losses = {f"{k}/test": v for k, v in losses.items()}

        self.log_dict(
            logged_losses,
            on_step=False,
            on_epoch=True,
        )

        if self.output_quantiles is not None:
            y_hat = self._quantiles_to_prediction(y_hat)

        return construct_ocf_ml_metrics_batch_df(batch, y, y_hat)

    def on_test_epoch_end(self, outputs):
        """Evalauate test results using oc_ml_metrics"""
        results_df = pd.concat(outputs)
        # setting model_name="test" gives us keys like "test/mw/forecast_horizon_30_minutes/mae"
        metrics = evaluation(results_df=results_df, model_name="test", outturn_unit="mw")

        self.log_dict(
            metrics,
        )

    def configure_optimizers(self):
        """Configure the optimizers using learning rate found with LR finder if used"""
        if self.lr is not None:
            # Use learning rate found by learning rate finder callback
            self._optimizer.lr = self.lr
        return self._optimizer(self.parameters())
