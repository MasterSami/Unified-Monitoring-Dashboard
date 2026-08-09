"""Zabbix host groups: show every group, resolve on old and new API versions.

Regression for hosts appearing group-less: the collector took only the first
group and used ``selectGroups`` (renamed to ``selectHostGroups`` in Zabbix 6.2+,
removed in 7.0). It must join all groups and fall back across versions.
"""

from __future__ import annotations

from app.collectors.base import CollectorError
from app.collectors.zabbix import ZabbixCollector
from app.config import Settings
from app.servers import ServerConfig

HOST_ROW_NEW = {
    "hostid": "1", "host": "srv1", "name": "srv1", "status": "0", "available": "1",
    "interfaces": [{"ip": "10.0.0.1", "main": "1", "available": "1"}],
    "hostgroups": [{"name": "Linux servers"}, {"name": "DB"}],
}
HOST_ROW_OLD = {
    "hostid": "2", "host": "srv2", "name": "srv2", "status": "0", "available": "1",
    "interfaces": [{"ip": "10.0.0.2", "main": "1", "available": "1"}],
    "groups": [{"name": "Telco"}, {"name": "Prod"}],
}


def _collector(rpc) -> ZabbixCollector:
    cfg = ServerConfig(name="Zabbix-34", platform="zabbix", url="http://z")
    c = ZabbixCollector(cfg, Settings(mock_mode=False))
    c._rpc = rpc  # type: ignore[assignment]
    return c


def test_all_groups_joined_on_new_api():
    def rpc(method, params):
        if method == "host.get":
            assert "selectHostGroups" in params  # new name tried first
            return [HOST_ROW_NEW]
        return []  # item.get for metrics
    hosts = _collector(rpc).collect_hosts()
    assert hosts[0]["group_name"] == "Linux servers, DB"


def test_falls_back_to_selectgroups_on_old_api():
    def rpc(method, params):
        if method == "host.get":
            if "selectHostGroups" in params:
                raise CollectorError("Invalid parameter selectHostGroups")
            return [HOST_ROW_OLD]
        return []
    hosts = _collector(rpc).collect_hosts()
    assert hosts[0]["group_name"] == "Telco, Prod"
