"""Pure relationship rules shared by chat and story events.

聊天里的关系演化已经下线：模型不再输出关系标记，adapter 也不再写库。
这里只保留剧情用的状态模型，剧情选项的 effect 仍会推进它。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Relationship(StrictModel):
    affinity: int = Field(default=20, ge=0, le=100)
    trust: int = Field(default=10, ge=0, le=100)
    route: Literal['open', 'friendship', 'romance'] = 'open'
    milestones: list[str] = Field(default_factory=list, max_length=80)
    impression: str = Field(default='', max_length=200)

    @property
    def stage(self) -> str:
        if self.route == 'romance' and 'mutual_romance' in self.milestones:
            return '恋人'
        if self.affinity >= 65 and self.trust >= 60 and 'shared_experience' in self.milestones:
            return '亲近'
        if self.affinity >= 45 and self.trust >= 40 and 'shared_experience' in self.milestones:
            return '信任'
        if self.affinity >= 30 and self.trust >= 20:
            return '熟悉'
        return '初识'
