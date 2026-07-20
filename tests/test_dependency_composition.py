"""Contratos de composição compatíveis para serviços centrais do app."""

from unittest.mock import Mock, sentinel

import pytest

from quimera.app.dispatch import AppDispatchServices, DispatchDependencies
from quimera.app.system_layer import AppSystemLayer, SystemLayerDependencies


def test_dispatch_from_dependencies_preserves_contract() -> None:
    get_agent_client = Mock(return_value=sentinel.agent_client)
    dependencies = DispatchDependencies(
        renderer=sentinel.renderer,
        prompt_builder=sentinel.prompt_builder,
        get_agent_client=get_agent_client,
        max_retries=7,
    )

    service = AppDispatchServices.from_dependencies(dependencies)

    assert service._dependencies is dependencies
    assert service._renderer is sentinel.renderer
    assert service._prompt_builder is sentinel.prompt_builder
    assert service._max_retries == 7
    assert service._get_agent_client() is sentinel.agent_client


def test_dispatch_legacy_constructor_remains_supported() -> None:
    service = AppDispatchServices(renderer=sentinel.renderer, max_retries=5)

    assert service._renderer is sentinel.renderer
    assert service._max_retries == 5
    assert service._dependencies.renderer is sentinel.renderer


def test_dispatch_factory_rejects_invalid_contract() -> None:
    with pytest.raises(TypeError, match="DispatchDependencies"):
        AppDispatchServices.from_dependencies(object())


def test_system_layer_from_dependencies_preserves_contract() -> None:
    dependencies = SystemLayerDependencies(
        agent_pool=sentinel.agent_pool,
        display_service=sentinel.display_service,
        prompt_builder=sentinel.prompt_builder,
        workspace_policy_getter=sentinel.policy_getter,
    )

    layer = AppSystemLayer.from_dependencies(dependencies)

    assert layer.agent_pool is sentinel.agent_pool
    assert layer._display is sentinel.display_service
    assert layer._prompt_builder is sentinel.prompt_builder
    assert layer.workspace_policy_getter is sentinel.policy_getter


def test_system_layer_legacy_constructor_remains_supported() -> None:
    layer = AppSystemLayer(
        agent_pool=sentinel.agent_pool,
        display_service=sentinel.display_service,
        prompt_builder=sentinel.prompt_builder,
    )

    assert layer.agent_pool is sentinel.agent_pool
    assert layer._display is sentinel.display_service
    assert layer._prompt_builder is sentinel.prompt_builder


def test_system_layer_factory_rejects_invalid_contract() -> None:
    with pytest.raises(TypeError, match="SystemLayerDependencies"):
        AppSystemLayer.from_dependencies(object())
