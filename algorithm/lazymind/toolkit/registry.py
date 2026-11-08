"""One catalog for internal ToolConfig registration and the external tool gateway."""

from dataclasses import replace

from . import data, documents, media, presentations, research, resources


# Core's knowledge.* / cloud_document.* and existing web_search, calculator,
# image_generator, image_editor, video_generator remain the authoritative services.
OPERATIONS = (
    ('resource.read', '读取资源', 'L1', resources.resource_read),
    ('resource.search', '搜索资源内容', 'L1', resources.resource_search),
    ('document.parse', '解析文档', 'L1', resources.document_parse),
    ('resource.images.extract', '提取文档图片', 'L1', resources.resource_images_extract),
    ('url_fetch', '读取网页', 'L1', research.url_fetch),
    ('image.search', '搜索图片', 'L1', media.image_search),
    ('github.pr.list', '收集 GitHub PR', 'L1', research.github_pr_list),
    ('github.pr.read', '读取 GitHub PR 描述', 'L1', research.github_pr_read),
    ('image.inspect', '检查图片', 'L1', media.image_inspect),
    ('image.caption', '添加图片文字', 'L1', media.image_caption),
    ('image.compose', '组合图片', 'L1', media.image_compose),
    ('video_to_gif', '视频转 GIF', 'L1', media.video_to_gif),
    ('document.convert', '转换文档结构', 'L1', documents.document_convert),
    ('document.export', '导出文档', 'L1', documents.document_export),
    ('document.patch.validate', '校验文档修改', 'L1', documents.document_patch_validate),
    ('document.patch.apply', '应用文档修改', 'L1', documents.document_patch_apply),
    ('presentation.inspect', '检查演示文稿', 'L2', presentations.presentation_inspect),
    ('presentation.fill', '填充演示模板', 'L2', presentations.presentation_fill),
    ('presentation.create', '创建演示文稿', 'L2', presentations.presentation_create),
    ('presentation.edit', '修改演示文稿', 'L2', presentations.presentation_edit),
    ('presentation.render', '渲染演示文稿', 'L2', presentations.presentation_render),
    ('presentation.export', '导出演示文稿', 'L2', presentations.presentation_export),
    ('academic.search', '搜索论文', 'L2', research.academic_search),
    ('academic.read', '读取论文全文', 'L2', research.academic_read),
    ('academic.resolve', '解析论文标识', 'L2', research.academic_resolve),
    ('academic.relations.validate', '校验论文关系结构', 'L2', research.academic_relations_validate),
    ('data.statistics', '计算统计量', 'L2', data.data_statistics),
    ('chart.render', '绘制图表', 'L2', data.chart_render),
)


def register_shared_tools(config_type, configs):
    """Replace transport-bound variants, preserving public names and the common tool pipeline."""
    from lazyllm.tools import ToolManager

    names = {item[0] for item in OPERATIONS}
    previous = {config.name: config for config in configs if config.name in names}
    configs[:] = [config for config in configs if config.name not in names]
    for name, label, layer, function in OPERATIONS:
        manager = ToolManager([function])
        schema = next(iter(manager.tools_info.values())).params_schema.model_json_schema()
        if name in previous:
            configs.append(replace(previous[name], tool=function, description=function.__doc__,
                                   description_en=function.__doc__, layer=layer, shared=True,
                                   input_schema=schema, output_schema={'type': 'object'}))
            continue
        configs.append(config_type(name=name, label=label, description=function.__doc__, tool=function,
                                   module='content', label_en=name, description_en=function.__doc__,
                                   capability_id=name, layer=layer, shared=True, input_schema=schema,
                                   output_schema={'type': 'object'}))
