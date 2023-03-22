import logging

from ocf_datapipes.utils.consts import BatchKey
from torch import nn
import torch

from pvnet.models.base_model import BaseModel

class Model(BaseModel):
    name = "single_value"

    def __init__(
        self,
        forecast_minutes: int = 120,
        history_minutes: int = 60,
    ):
        """
        Simple baseline model that predicts always the same value. Mainly used for testing
        """
        self.forecast_minutes = forecast_minutes
        self.history_minutes = history_minutes
        super().__init__()
        self._dummy_parameters = nn.Parameter(torch.zeros(1), requires_grad=True)

    def forward(self, x: dict):

        # Returns a single value at all steps
        y_hat = torch.zeros_like(x[BatchKey.gsp][:, :self.forecast_len]) + self._dummy_parameters

        return y_hat