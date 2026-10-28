from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..llm.base import LlmError, LlmProvider
from ..storage.llm_calls import LlmCallRecord, LlmCallRepository
from .models import ApplyError, Materials, VacancyDossier

PLACEHOLDER = re.compile("(?:❗️?)+\\s*ВСТАВИТЬ\\s*(?:❗️?)+")
_PDF_LINE = re.compile(r"^PDF: (.+?)\s+pages=(\d+)", re.M)

APP_RULES = """You are running inside the Vacancy Retriever app, without tools: you cannot run commands or read files.
Everything the resume-tailor skill would make you look up is given below: SKILL.md, references/letters.md,
references/projects_facts.md and the output of `build.py catalog`. The app runs `build.py build` itself.

The user has explicitly asked for BOTH the tailored resume and a cover letter for the vacancy in the user message
(the short cover-letter type, unless the vacancy is an internship with a selection process). Follow the skill's
decisions and honesty rules exactly.

If the company is not stated, never write "Unknown" (or "unknown", «Неизвестно») anywhere: leave "company" empty
("") in the selection, greet neutrally (RU «Добрый день!», EN «Dear Hiring Team,») and do not name a company in the
letter or the CV title.

Reply with ONE JSON object and nothing else:
{"selection": <selection.json exactly as in the skill's schema, valid JSON without comments>,
 "letter": "<the letter as plain text without a code fence, ending with the verbatim Резюме/CV ❗️ВСТАВИТЬ❗️ and GitHub lines>",
 "skills": [<3-%(max)d key hard skills / tools the VACANCY requires, short canonical names (Python, SQL, A/B testing),
            most important first, in the vacancy's terms — not limited to the candidate's skills>]}"""


class ResumeSkill:
    """The user's resume-tailor Claude skill: its instructions are the prompt, its build.py makes the PDF."""

    def __init__(self, skill_dir: Path, tex_bin_dir: Path | None = None, tex_cache_dir: Path | None = None):
        self.skill_dir = skill_dir
        self.tex_bin_dir = tex_bin_dir
        self.tex_cache_dir = tex_cache_dir

    @property
    def script(self) -> Path:
        return self.skill_dir / "scripts" / "build.py"

    def _check(self) -> None:
        if not self.script.exists():
            raise ApplyError(f"resume-tailor skill not found in {self.skill_dir} (config/apply.yaml → resume.skill_dir)",
                             kind="setup")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.tex_bin_dir:
            env["PATH"] = f"{self.tex_bin_dir}{os.pathsep}{env.get('PATH', '')}"
        if self.tex_cache_dir:  # Tectonic's packages stay in the project (.cache/tectonic), not in ~/Library/Caches
            env["TECTONIC_CACHE_DIR"] = str(self.tex_cache_dir)
        return env

    def _run(self, *args: str, timeout: int) -> str:
        self._check()
        result = subprocess.run([sys.executable, str(self.script), *args], capture_output=True, text=True,
                                env=self._env(), timeout=timeout, cwd=self.skill_dir)
        if result.returncode != 0:
            raise ApplyError(f"build.py {args[0]} failed: {(result.stdout + result.stderr).strip()[-400:]}")
        return result.stdout

    def instructions(self) -> str:
        self._check()
        parts = []
        for name in ("SKILL.md", "references/letters.md", "references/projects_facts.md"):
            path = self.skill_dir / name
            if path.exists():
                parts.append(f"===== {name} =====\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    def catalog(self) -> str:
        return self._run("catalog", timeout=60)

    def build(self, selection: dict[str, Any], out_dir: Path) -> tuple[Path, str | None]:
        """Compiles the one-page CV. Returns the PDF and a warning (e.g. it did not fit one page)."""
        path = self._env()["PATH"]
        if not (shutil.which("tectonic", path=path) or shutil.which("xelatex", path=path)):
            raise ApplyError("Neither tectonic nor xelatex found: brew install tectonic (docs/APPLY.md)", kind="setup")
        out_dir.mkdir(parents=True, exist_ok=True)
        # The skill's own schema shows "section_order": null, but build.py fails on a null: optional keys are dropped.
        selection = {key: value for key, value in selection.items() if value is not None}
        if str(selection.get("company", "")).strip().casefold() in {"unknown", "неизвестно", "n/a", "na", "-"}:
            selection["company"] = ""  # never «Unknown» in the file name
        selection_path = out_dir / "selection.json"
        selection_path.write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
        output = self._run("build", str(selection_path), "--out", str(out_dir), timeout=300)
        match = _PDF_LINE.search(output)
        if not match or not Path(match.group(1)).exists():
            raise ApplyError(f"build.py produced no PDF: {output.strip()[-400:]}")
        pdf = Path(match.group(1))
        if "__" in pdf.stem:  # no company: Ismailov_CV__DA.pdf → Ismailov_CV_DA.pdf
            tidy = pdf.with_name(re.sub(r"_{2,}", "_", pdf.stem).strip("_") + pdf.suffix)
            pdf.replace(tidy)
            for other in pdf.parent.glob(f"{pdf.stem}.*"):
                other.replace(other.with_name(tidy.stem + other.suffix))
            return tidy, "The CV does not fit one page" if int(match.group(2)) > 1 else None
        warning = "The CV does not fit one page" if int(match.group(2)) > 1 else None
        return Path(match.group(1)), warning


class MaterialsWriter:
    """One LLM call per vacancy: CV selection + cover letter + the vacancy's key skills."""

    def __init__(self, provider: LlmProvider, model: str, skill: ResumeSkill, calls: LlmCallRepository,
                 max_skills: int = 10, max_input_chars: int = 9000):
        self._provider = provider
        self._model = model
        self._skill = skill
        self._calls = calls
        self._max_skills = max_skills
        self._max_input_chars = max_input_chars

    def system_prompt(self) -> str:
        return "\n\n".join([APP_RULES % {"max": self._max_skills}, self._skill.instructions(),
                            f"===== build.py catalog =====\n{self._skill.catalog()}"])

    def user_prompt(self, dossier: VacancyDossier) -> str:
        head = [f"Vacancy: {dossier.title}"]
        if dossier.employer:  # an unknown employer is simply not mentioned (never «Unknown»)
            head.append(f"Company: {dossier.employer}")
        else:
            head.append("Company: not stated in the vacancy")
        if dossier.company_hint:
            head.append(dossier.company_hint)
        return "\n".join([*head, "", dossier.text[: self._max_input_chars]])

    async def write(self, dossier: VacancyDossier) -> Materials:
        system, user = self.system_prompt(), self.user_prompt(dossier)
        started = time.monotonic()
        try:
            response = await self._provider.complete_json(system, user, self._model)
        except LlmError as exc:
            self._record(started, None, str(exc))
            kind = {"limit": "limit", "auth": "setup"}.get(exc.kind, "other")
            raise ApplyError(f"LLM: {exc}", kind=kind) from exc
        self._record(started, response, None)
        return self.parse(response.data, self._max_skills)

    @staticmethod
    def parse(data: dict[str, Any], max_skills: int = 10) -> Materials:
        selection, letter = data.get("selection"), data.get("letter")
        if not isinstance(selection, dict) or not {"lang", "company", "role_abbr"} <= selection.keys():
            raise ApplyError("LLM reply has no valid selection (lang, company, role_abbr)")
        if not isinstance(letter, str) or not letter.strip():
            raise ApplyError("LLM reply has no cover letter")
        skills = [str(s).strip() for s in data.get("skills") or [] if str(s).strip()]
        return Materials(selection, letter.strip(), skills[:max_skills])

    def _record(self, started: float, response: Any, error: str | None) -> None:
        usage = response.usage if response else None
        self._calls.record(LlmCallRecord(
            run_id=None, stage="resume", provider=self._provider.name, model=(usage.model if usage else None) or self._model,
            n_items=1, input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cache_read_tokens=usage.cache_read_tokens if usage else None,
            cache_creation_tokens=usage.cache_creation_tokens if usage else None,
            ok=error is None, duration_s=time.monotonic() - started, error=error,
            tokens_unknown=not (usage and usage.known), auth_source=response.auth_source if response else None))


def insert_cv_link(letter: str, pdf_url: str | None) -> str:
    """The skill leaves «Резюме: ❗️…ВСТАВИТЬ…❗️»; the link to the PDF on Drive goes there."""
    return PLACEHOLDER.sub(pdf_url, letter, count=1) if pdf_url else letter
