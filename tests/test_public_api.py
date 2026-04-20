import quimera.app as app_module


def test_quimera_app_public_api_surface_is_stable():
    assert app_module.__all__ == ["QuimeraApp", "logger", "PromptAwareStderrHandler"]
    assert app_module.QuimeraApp.__name__ == "QuimeraApp"
    assert app_module.logger.name == "quimera.staging"
    assert app_module.PromptAwareStderrHandler.__name__ == "PromptAwareStderrHandler"
    # Extra sanity checks to ensure public API surface remains accessible
    assert hasattr(app_module, "QuimeraApp")
    assert hasattr(app_module, "logger")
    assert hasattr(app_module, "PromptAwareStderrHandler")
