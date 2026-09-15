"""CPU readings that do not fit in 0-100, and the item we pick to avoid them.

Reported from production: RPA-Prod4, a 2-vCPU VMware guest, showed 137% CPU on
the Capacity table while Zabbix's own Latest data read 65.135%. Two separate
faults, both here.
"""

from __future__ import annotations

import pytest

from app.zabbix_report import _classify, normalize_cpu_pct

# The eight CPU items RPA-Prod4 actually carries, from the VMware VM template.
RPA_PROD4_ITEMS = [
    {"itemid": "1", "key_": "vmware.vm.cpu.latency.perf", "name": "CPU latency in percent"},
    {"itemid": "2", "key_": "vmware.vm.cpu.readiness", "name": "CPU readiness latency in percent"},
    {"itemid": "3", "key_": "vmware.vm.cpu.ready", "name": "CPU ready"},
    {"itemid": "4", "key_": "vmware.vm.cpu.swapwait", "name": "CPU swap-in latency in percent"},
    {"itemid": "5", "key_": "vmware.vm.cpu.usage", "name": "CPU usage"},
    {"itemid": "6", "key_": "vmware.vm.cpu.usage.perf", "name": "CPU usage in percent"},
    {"itemid": "7", "key_": "vmware.vm.cpu.utilization", "name": "CPU utilization"},
    {"itemid": "8", "key_": "vmware.vm.cpu.num", "name": "Number of virtual CPUs"},
]


class TestWhichItemWeRead:
    """A VMware guest carries two CPU percentages that mean different things."""

    def test_utilization_beats_usage_in_percent(self):
        """The fault that produced 137%.

        "CPU utilization" is normalized to 0-100. "CPU usage in percent" sums
        across vCPUs the way top does, so the same 2-vCPU guest reads 130%.
        Ranking put the second one first.
        """
        picked = _classify(RPA_PROD4_ITEMS)["cpu_util"]
        assert picked["name"] == "CPU utilization"

    def test_the_vmware_vcpu_count_is_found(self):
        """The second fault, and the one that made the first unrecoverable.

        Core count was only matched on the Linux `system.cpu.num` key or the
        name "CPU cores". On a VMware guest neither matches, so there was no
        divisor to convert a summed reading back to a percentage.
        """
        cores = _classify(RPA_PROD4_ITEMS)["cpu_num"]
        assert cores is not None
        assert cores["name"] == "Number of virtual CPUs"

    def test_a_linux_host_is_unaffected(self):
        """The key still wins over any name, as it always did."""
        items = [
            {"itemid": "1", "key_": "x", "name": "CPU usage in percent"},
            {"itemid": "2", "key_": "system.cpu.util", "name": "CPU utilization"},
            {"itemid": "3", "key_": "system.cpu.num", "name": "CPU cores"},
        ]
        classified = _classify(items)
        assert classified["cpu_util"]["key_"] == "system.cpu.util"
        assert classified["cpu_num"]["key_"] == "system.cpu.num"

    def test_idle_style_keys_still_outrank_names(self):
        items = [
            {"itemid": "1", "key_": "x", "name": "CPU utilization"},
            {"itemid": "2", "key_": "system.cpu.util[,user]", "name": "User CPU"},
        ]
        assert _classify(items)["cpu_util"]["itemid"] == "2"


class TestNormalising:
    """Whatever item we end up on, the column has to stay a percentage."""

    def test_the_production_reading_converts_back(self):
        pct, raw = normalize_cpu_pct(130.3, 2)
        assert pct == pytest.approx(65.15, abs=0.01)
        assert raw == 130.3            # the original is kept, not discarded

    def test_a_normal_reading_is_left_alone(self):
        pct, raw = normalize_cpu_pct(65.135, 2)
        assert pct == 65.135
        assert raw is None             # nothing was adjusted

    @pytest.mark.parametrize(
        "value, cores, expected",
        [
            (137.0, 2, 68.5),
            (395.0, 4, 98.75),
            (780.0, 8, 97.5),
            (100.0, 4, 100.0),         # exactly at the ceiling, untouched
            (0.0, 2, 0.0),
        ],
    )
    def test_conversion_across_core_counts(self, value, cores, expected):
        assert normalize_cpu_pct(value, cores)[0] == pytest.approx(expected)

    def test_an_unexplainable_reading_is_capped_not_shown_raw(self):
        """Over 100 with no core count to divide by, or still over after.

        Capping keeps the bar inside its track and stops the host sorting above
        genuinely saturated machines. The original is returned so the detail
        panel can still name it.
        """
        assert normalize_cpu_pct(150.0, None) == (100.0, 150.0)
        assert normalize_cpu_pct(150.0, 1) == (100.0, 150.0)
        assert normalize_cpu_pct(900.0, 2) == (100.0, 900.0)

    def test_nothing_in_nothing_out(self):
        assert normalize_cpu_pct(None, 2) == (None, None)
        assert normalize_cpu_pct(None, None) == (None, None)

    def test_no_reading_survives_above_one_hundred(self):
        """The guarantee the Capacity column depends on."""
        for value in (0, 55.5, 100, 130.3, 137, 395, 1000):
            for cores in (None, 1, 2, 4, 8, 64):
                pct, _raw = normalize_cpu_pct(float(value), cores)
                assert 0 <= pct <= 100, (value, cores, pct)
