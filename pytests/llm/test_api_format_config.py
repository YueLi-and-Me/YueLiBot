"""模型协议合法组合在加载期拒绝，错误必须指出模型与任务。"""

import pytest

from src.core.config.loader import _build_routing
from src.core.config.schema import ApiProviderConfig, ModelCatalog, ModelDefinitionConfig
from src.core.config.schema import CONFIG_VERSION


@pytest.mark.parametrize('task,api_format', [('embedding', 'responses'), ('chat', 'dashscope_multimodal')])
def test_invalid_task_protocol(task, api_format):
    model = ModelDefinitionConfig(name='测试模型', model_identifier='model', api_provider='测试厂商', api_format=api_format)
    catalog = ModelCatalog.model_validate({
        'inner': {'version': CONFIG_VERSION}, 'models': [model],
        'model_tasks': {task: {'model_list': [model.name]}},
    })
    provider = ApiProviderConfig(name='测试厂商', kind='openai', api_key='fake')
    with pytest.raises(ValueError) as exc:
        _build_routing(task, catalog, {model.name: model}, {provider.name: provider}, None)
    assert task in str(exc.value)
    assert model.name in str(exc.value)

