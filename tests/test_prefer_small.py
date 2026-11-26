from types import SimpleNamespace

from src.services.channel_service import ChannelService


def _ch(name, viewers):
    return SimpleNamespace(name=name, viewers=viewers)


CHANNELS = [_ch("big", 5000), _ch("offline", None), _ch("small", 3), _ch("mid", 200)]


def test_default_biggest_first_offline_last():
    assert [c.name for c in ChannelService.order_by_viewers(CHANNELS)] == ["big", "mid", "small", "offline"]


def test_prefer_small_fewest_first_offline_still_last():
    assert [c.name for c in ChannelService.order_by_viewers(CHANNELS, True)] == ["small", "mid", "big", "offline"]
