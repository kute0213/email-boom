"""定时计算：Cron 表达式解析 / 各种运行模式的下次执行时间。"""
from datetime import datetime, timedelta

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

RUN_MODE_HINT = {
    "manual": "手动执行",
    "once": "定时单次",
    "cron": "Cron 表达式",
    "interval": "固定间隔重复",
}

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)


def _parse_value(token: str, lo: int, hi: int, names: dict) -> int:
    token = token.strip().lower()
    if token in names:
        token = str(names[token])
    try:
        v = int(token)
    except ValueError:
        raise ValueError(f"无效的数值：{token}")
    if v < lo or v > hi:
        raise ValueError(f"数值超出范围 {lo}-{hi}：{v}")
    return v


def _parse_field(raw: str, lo: int, hi: int, names: dict = None) -> set:
    names = names or {}
    values = set()
    if not raw:
        raise ValueError("cron 表达式字段为空")
    for part in raw.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = _parse_value(step_s, 1, hi - lo + 1, {})
            if step < 1:
                raise ValueError("步长必须 >= 1")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start = _parse_value(a, lo, hi, names)
            end = _parse_value(b, lo, hi, names)
            if start > end:
                raise ValueError(f"范围起点大于终点：{part}")
        else:
            v = _parse_value(part, lo, hi, names)
            start = end = v
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"字段没有匹配任何值：{raw}")
    return values


class CronExpr:
    """标准 5 段 cron：分 时 日 月 周。"""

    def __init__(self, expr: str):
        parts = (expr or "").split()
        if len(parts) != 5:
            raise ValueError("Cron 需要 5 段：分 时 日 月 周（例如 0 8 * * 1-5）")
        self.expr = " ".join(parts)
        self.minute = _parse_field(parts[0], 0, 59)
        self.hour = _parse_field(parts[1], 0, 23)
        self.day = _parse_field(parts[2], 1, 31)
        self.month = _parse_field(parts[3], 1, 12, _MONTHS)
        dow = _parse_field(parts[4], 0, 7, _DAYS)
        self.dow = {6 if d == 7 else d for d in dow}
        self.day_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def _day_matches(self, dt: datetime) -> bool:
        if dt.month not in self.month:
            return False
        cron_dow = (dt.weekday() + 1) % 7  # python 周一=0 → cron 周日=0
        dom_ok = dt.day in self.day
        dow_ok = cron_dow in self.dow
        # 与 Vixie cron 一致：日、周都被限定时取“或”，否则取“与”
        if not self.day_any and not self.dow_any:
            return dom_ok or dow_ok
        if self.day_any and self.dow_any:
            return True
        if self.day_any:
            return dow_ok
        return dom_ok

    def next_after(self, after: datetime) -> datetime:
        """返回严格晚于 after 的下一次触发时刻（分钟精度），找不到返回 None。"""
        start = (after.replace(second=0, microsecond=0) + timedelta(minutes=1))
        hours = sorted(self.hour)
        minutes = sorted(self.minute)
        for day_offset in range(0, 366 * 2):
            day = (start + timedelta(days=day_offset)).date()
            probe = datetime(day.year, day.month, day.day)
            if probe.month not in self.month or not self._day_matches(probe):
                continue
            for h in hours:
                for m in minutes:
                    cand = datetime(day.year, day.month, day.day, h, m)
                    if cand > after:
                        return cand
        return None


def validate(expr: str) -> CronExpr:
    return CronExpr(expr)


def next_run_for(task: dict, now: datetime = None) -> tuple[str | None, bool]:
    """根据任务配置计算下次运行时刻。

    返回 (时刻字符串或 None, 是否已达最大轮次)。
    None 且未达上限 => 无需再调度。
    """
    now = now or datetime.now()
    mode = task.get("run_mode", "manual")
    max_runs = int(task.get("max_runs") or 0)
    run_count = int(task.get("run_count") or 0)
    exhausted = max_runs > 0 and run_count >= max_runs
    if exhausted:
        return None, True

    if mode == "once":
        raw = (task.get("schedule_at") or "").strip()
        if not raw:
            return None, False
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                raise ValueError("单次执行时间格式应为 YYYY-MM-DD HH:MM")
        return dt.strftime("%Y-%m-%d %H:%M:%S"), False

    if mode == "cron":
        try:
            c = CronExpr(task.get("cron_expr") or "")
        except ValueError as e:
            raise ValueError(f"Cron 表达式错误：{e}")
        nxt = c.next_after(now)
        return (nxt.strftime("%Y-%m-%d %H:%M:%S"), False) if nxt else (None, False)

    if mode == "interval":
        minutes = max(1, int(task.get("interval_minutes") or 60))
        return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"), False

    return None, False  # manual
