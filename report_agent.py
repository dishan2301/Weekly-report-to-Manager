from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from config import Settings


class ReportAgent:
    def __init__(self, settings: Settings, prompt_path: Path | None = None):
        self.settings = settings
        self.prompt_path = prompt_path or Path("prompts/weekly_report_prompt.txt")

    def generate(self, notes: str, additional_instructions: str = "") -> str:
        notes = notes.strip()
        if not notes:
            raise ValueError("Please enter at least one work note.")
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        instructions = self.prompt_path.read_text(encoding="utf-8")
        if self.settings.sender_name.strip():
            instructions += (
                "\n- End the email exactly with: Regards, followed by "
                + self.settings.sender_name.strip()
                + "."
            )
        user_input = f"Original weekly notes:\n{notes}"
        if additional_instructions.strip():
            user_input += (
                "\n\nUser-provided editing instructions or additional facts:\n"
                + additional_instructions.strip()
            )

        client = OpenAI(api_key=self.settings.openai_api_key)
        response = client.responses.create(
            model=self.settings.openai_model,
            instructions=instructions,
            input=user_input,
            store=False,
        )
        body = response.output_text.strip()
        if not body:
            raise RuntimeError("OpenAI returned an empty report.")
        return body
