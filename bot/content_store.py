from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.content import COURSE_OVERVIEW_TEXT, LESSON_SHOWCASE, START_PHOTO_URL, START_TEXT


@dataclass(frozen=True)
class LessonContent:
    text: str
    photos: tuple[str, ...]
    typing_before_seconds: int = 0


@dataclass(frozen=True)
class DynamicContent:
    start_text: str
    start_photos: tuple[str, ...]
    course_overview_text: str
    lessons: tuple[LessonContent, ...]
    course_price_rub: int


def split_photo_sources(raw_text: str) -> list[str]:
    sources: list[str] = []
    for line in raw_text.replace(",", "\n").splitlines():
        item = line.strip()
        if item:
            sources.append(item)
    return sources


class ContentStore:
    def __init__(self, path: str, upload_dir: str, default_course_price_rub: int) -> None:
        self.path = Path(path)
        self.upload_dir = Path(upload_dir)
        self.default_course_price_rub = max(1, int(default_course_price_rub))
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def get_content(self) -> DynamicContent:
        state = self._state
        lessons = tuple(
            LessonContent(
                text=str(item["text"]),
                photos=tuple(item["photos"]),
                typing_before_seconds=int(item["typing_before_seconds"]),
            )
            for item in state["lessons"]
        )
        return DynamicContent(
            start_text=str(state["start_text"]),
            start_photos=tuple(state["start_photos"]),
            course_overview_text=str(state["course_overview_text"]),
            lessons=lessons,
            course_price_rub=int(state["course_price_rub"]),
        )

    def update(
        self,
        start_text: str,
        start_photos: list[str],
        course_overview_text: str,
        lessons: list[dict[str, Any]],
        course_price_rub: int,
    ) -> None:
        normalized_lessons: list[dict[str, Any]] = []
        for lesson in lessons:
            photos = [str(p).strip() for p in lesson.get("photos", []) if str(p).strip()]
            typing = int(lesson.get("typing_before_seconds", 0))
            if typing < 0:
                typing = 0
            normalized_lessons.append(
                {
                    "text": str(lesson.get("text", "")),
                    "photos": photos,
                    "typing_before_seconds": typing,
                }
            )

        self._state = {
            "start_text": start_text,
            "start_photos": [str(p).strip() for p in start_photos if str(p).strip()],
            "course_overview_text": course_overview_text,
            "lessons": normalized_lessons,
            "course_price_rub": max(1, int(course_price_rub)),
        }
        self._persist_state()

    def _default_state(self) -> dict[str, Any]:
        start_photos = [START_PHOTO_URL] if START_PHOTO_URL else []
        return {
            "start_text": START_TEXT,
            "start_photos": start_photos,
            "course_overview_text": COURSE_OVERVIEW_TEXT,
            "lessons": [
                {
                    "text": lesson.text,
                    "photos": list(lesson.photos),
                    "typing_before_seconds": lesson.typing_before_seconds,
                }
                for lesson in LESSON_SHOWCASE
            ],
            "course_price_rub": self.default_course_price_rub,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            state = self._default_state()
            self._persist_raw(state)
            return state

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        return self._normalize(raw)

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        default = self._default_state()
        if not isinstance(raw, dict):
            return default

        start_text = str(raw.get("start_text", default["start_text"]))
        course_overview_text = str(raw.get("course_overview_text", default["course_overview_text"]))
        raw_course_price = raw.get("course_price_rub", default["course_price_rub"])
        try:
            course_price_rub = max(1, int(raw_course_price))
        except Exception:
            course_price_rub = int(default["course_price_rub"])

        raw_start_photos = raw.get("start_photos", default["start_photos"])
        start_photos = [str(p).strip() for p in raw_start_photos if str(p).strip()] if isinstance(raw_start_photos, list) else list(default["start_photos"])

        raw_lessons = raw.get("lessons", default["lessons"])
        lessons: list[dict[str, Any]] = []
        if isinstance(raw_lessons, list):
            for idx, item in enumerate(raw_lessons):
                default_lesson = default["lessons"][idx] if idx < len(default["lessons"]) else default["lessons"][-1]
                if not isinstance(item, dict):
                    item = {}
                photos_raw = item.get("photos", default_lesson["photos"])
                photos = [str(p).strip() for p in photos_raw if str(p).strip()] if isinstance(photos_raw, list) else list(default_lesson["photos"])
                typing = item.get("typing_before_seconds", default_lesson["typing_before_seconds"])
                try:
                    typing_int = max(0, int(typing))
                except Exception:
                    typing_int = int(default_lesson["typing_before_seconds"])
                lessons.append(
                    {
                        "text": str(item.get("text", default_lesson["text"])),
                        "photos": photos,
                        "typing_before_seconds": typing_int,
                    }
                )

        if not lessons:
            lessons = default["lessons"]

        normalized = {
            "start_text": start_text,
            "start_photos": start_photos,
            "course_overview_text": course_overview_text,
            "lessons": lessons,
            "course_price_rub": course_price_rub,
        }
        self._persist_raw(normalized)
        return normalized

    def _persist_state(self) -> None:
        self._persist_raw(self._state)

    def _persist_raw(self, state: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
