from dataclasses import dataclass, field
from time import monotonic

from astrbot.api import logger


@dataclass
class QueryTiming:
    action: str
    started: float = field(default_factory=monotonic)
    progress: float = 0.0
    queue: float = 0.0
    data: float = 0.0
    render: float = 0.0
    card_hit: bool = False
    image_hit: bool = False
    image_bytes: int = 0
    send_started: float = 0.0
    queue_started: float | None = None

    def finish_queue(self):
        if self.queue_started is not None:
            self.queue += monotonic() - self.queue_started
            self.queue_started = None

    def log(self, event, send_status: str):
        scope = "群聊" if event.get_group_id() else "私聊"
        now = monotonic()
        logger.info(
            f"名剑耗时 操作={self.action} 平台={event.get_platform_name()} 场景={scope} "
            f"进度={self.progress*1000:.0f}ms 取数排队={self.queue*1000:.0f}ms "
            f"取数={self.data*1000:.0f}ms 出图={self.render*1000:.0f}ms "
            f"发送阶段={(now-self.send_started)*1000:.0f}ms 总耗时={(now-self.started)*1000:.0f}ms "
            f"数据缓存={'命中' if self.card_hit else '-'} 图片缓存={'命中' if self.image_hit else '-'} "
            f"图片字节={self.image_bytes} 发送状态={send_status}"
        )
