import os
from typing import cast

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "false")

import gradio as gr

from .core.config import get_settings
from .modules.ai.gradio_console import WORKFLOW_CSS, build_workflow_console


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="{{PROJECT_NAME}} AI Workflow") as demo:
        build_workflow_console()
    return cast(gr.Blocks, demo)


def main() -> None:
    settings = get_settings()
    build_demo().launch(
        server_name=settings.gradio_host,
        server_port=settings.gradio_port,
        css=WORKFLOW_CSS,
        show_error=False,
    )


if __name__ == "__main__":
    main()
