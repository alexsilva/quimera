import quimera.app as app_module
from quimera.app.config import logger
from quimera.app.core import QuimeraApp
from quimera.app.handlers import PromptAwareStderrHandler


def test_quimera_app_public_api_surface_is_stable():
    assert app_module.__all__ == ["QuimeraApp", "logger", "PromptAwareStderrHandler"]
    assert tuple(app_module.__all__) == ("QuimeraApp", "logger", "PromptAwareStderrHandler")
    assert app_module.QuimeraApp.__name__ == "QuimeraApp"
    assert app_module.logger.name == "quimera.staging"
    assert app_module.PromptAwareStderrHandler.__name__ == "PromptAwareStderrHandler"
    assert app_module.QuimeraApp is QuimeraApp
    assert app_module.logger is logger
    assert app_module.PromptAwareStderrHandler is PromptAwareStderrHandler
    assert hasattr(app_module, "QuimeraApp")
    assert hasattr(app_module, "logger")
    assert hasattr(app_module, "PromptAwareStderrHandler")
