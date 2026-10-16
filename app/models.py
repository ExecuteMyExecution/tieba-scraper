from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CrawlRequest(BaseModel):
    forum_name: str = Field(min_length=1, max_length=64)
    pages: int = Field(default=3, ge=1, le=100)
    first_floor_image_filter: Literal[
        "all", "with_images", "without_images", "portrait_images"
    ] = "all"
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("forum_name")
    @classmethod
    def normalize_forum_name(cls, value: str) -> str:
        value = value.strip()
        if value.endswith("吧"):
            value = value[:-1].strip()
        if not value:
            raise ValueError("吧名不能为空")
        return value

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self
