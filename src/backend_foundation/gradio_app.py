"""Gradio project generator and optional AI development console."""

from __future__ import annotations

import os
from typing import cast

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "false")

import gradio as gr

from backend_foundation.cli import CliError, create_project_archive, slugify
from backend_foundation.core.config import get_settings
from backend_foundation.manifest import (
    ManifestError,
    load_catalog,
    load_profiles,
    resolve_manifest_modules,
)
from backend_foundation.modules.ai.graph import run_smoke_graph


async def run_smoke(value: str) -> str:
    if not value.strip():
        return "请输入测试内容"
    return await run_smoke_graph(value.strip())


def _selected_modules(profile: str, additional: list[str] | None) -> tuple[str, ...]:
    profiles = load_profiles()
    if profile not in profiles:
        raise CliError(f"Unknown profile: {profile}")
    requested = list(profiles[profile].modules)
    for module in additional or []:
        if module not in requested:
            requested.append(module)
    if "temporal" in requested and "jobs" not in requested:
        requested.append("jobs")
    return resolve_manifest_modules(tuple(requested), load_catalog())


def _effective_profile(modules: tuple[str, ...]) -> str:
    profiles = load_profiles()
    for profile_name, manifest in profiles.items():
        expected = resolve_manifest_modules(manifest.modules, load_catalog())
        if expected == modules:
            return profile_name
    return "custom"


def generate_project_zip(
    project_name: str,
    profile: str,
    additional_modules: list[str] | None,
) -> tuple[str, str | None, str]:
    """Generate a project archive for Gradio outputs."""
    try:
        slugify(project_name)
        modules = _selected_modules(profile, additional_modules)
        effective_profile = _effective_profile(modules)
        archive = create_project_archive(project_name, effective_profile, modules)
        module_text = ", ".join(modules)
        return (
            f"生成成功：`{archive.name}`（{effective_profile} profile）",
            str(archive),
            f"已安装模块：`{module_text}`",
        )
    except (CliError, ManifestError, OSError, ValueError) as error:
        return f"生成失败：{error}", None, ""


def _build_generator() -> None:
    gr.Markdown("## 全栈项目生成器")
    gr.Markdown("选择技术栈组合，生成可分别运行且默认联通的前端与后端项目 ZIP。")
    project_name = gr.Textbox(
        label="项目名称", value="my-service", placeholder="例如：order-service"
    )
    profile = gr.Dropdown(
        choices=["api", "workflow", "ai", "identity", "saas", "full"],
        value="api",
        label="基础 Profile",
    )
    additional_modules = gr.CheckboxGroup(
        choices=["temporal", "ai"],
        label="高级追加模块（会自动解析依赖）",
        info="例如在 api 上追加 temporal 会生成 workflow 组合。",
    )
    generate = gr.Button("生成并准备下载", variant="primary")
    status = gr.Markdown()
    modules = gr.Markdown()
    download = gr.File(label="下载项目 ZIP", interactive=False)
    generate.click(
        generate_project_zip,
        inputs=[project_name, profile, additional_modules],
        outputs=[status, download, modules],
    )


def _build_ai_console() -> None:
    settings = get_settings()
    gr.Markdown(f"## {settings.app_name} AI 调试台")
    with gr.Row():
        gr.Markdown(f"运行环境：`{settings.app_env}`")
        gr.Markdown(f"API 地址：`{settings.api_host}:{settings.api_port}`")
    value = gr.Textbox(label="Smoke Graph 输入", value="hello")
    output = gr.Textbox(label="输出", interactive=False)
    run = gr.Button("运行通用测试", variant="primary")
    run.click(run_smoke, inputs=value, outputs=output)


def build_demo() -> gr.Blocks:
    settings = get_settings()
    with gr.Blocks(title=f"{settings.app_name} 全栈项目生成器") as demo:
        with gr.Tab("全栈项目生成器"):
            _build_generator()
        with gr.Tab("AI 调试台"):
            _build_ai_console()
    return cast(gr.Blocks, demo)


def main() -> None:
    settings = get_settings()
    build_demo().launch(
        server_name=settings.gradio_host,
        server_port=settings.gradio_port,
        show_error=False,
    )


if __name__ == "__main__":
    main()
