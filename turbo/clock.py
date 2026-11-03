"""UTC stamps that sort as text. The clock is injectable."""

import calendar
import time


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))


def parse_stamp(value: str) -> float:
    return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
