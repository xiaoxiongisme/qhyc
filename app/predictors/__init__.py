"""M2/M3 模型库（PRD §5.2 全族）"""
from app.predictors.base import ModelOutput, BaseModel as PredictBaseModel
from app.predictors.fourier import FourierModel
from app.predictors.markov import MarkovModel
from app.predictors.arima_model import ARIMAModel
from app.predictors.wavelet import WaveletModel
from app.predictors.garch import GARCHModel
from app.predictors.kalman import KalmanModel
from app.predictors.hmm_model import HMMModel
from app.predictors.bayesian import BayesianModel
from app.predictors.montecarlo import MonteCarloModel
from app.predictors.ml_models import (
    RandomForestModel,
    GPRModel,
    XGBoostModel,
    LSTMModel,
)

MODEL_REGISTRY: dict[str, type] = {
    "fourier": FourierModel,
    "markov": MarkovModel,
    "arima": ARIMAModel,
    "wavelet": WaveletModel,
    "garch": GARCHModel,
    "kalman": KalmanModel,
    "hmm": HMMModel,
    "bayesian": BayesianModel,
    "montecarlo": MonteCarloModel,
    "rf": RandomForestModel,
    "gpr": GPRModel,
    "xgb": XGBoostModel,
    "lstm": LSTMModel,
}

__all__ = [
    "ModelOutput",
    "PredictBaseModel",
    "FourierModel",
    "MarkovModel",
    "ARIMAModel",
    "WaveletModel",
    "GARCHModel",
    "KalmanModel",
    "HMMModel",
    "BayesianModel",
    "MonteCarloModel",
    "RandomForestModel",
    "GPRModel",
    "XGBoostModel",
    "LSTMModel",
    "MODEL_REGISTRY",
]