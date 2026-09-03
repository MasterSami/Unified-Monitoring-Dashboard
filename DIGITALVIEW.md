# Huawei Digital View asset inventory

## Why it works this way

Huawei's i2000 / Digital View API port is closed to us, so there is no live
feed to collect. What there is, is the asset export the Digital View UI
produces — `BaseAssetImportTemplate_En.xlsx` — and that file holds the whole
estate: every VM, physical host, rack server and storage array, with its CPU,
memory, disks, rack position and application unit.

SAMI'X reads that workbook. Point it at the file, and 244 Huawei assets show up
alongside Zabbix, Dynatrace and NNMi on the Hosts, Capacity and Shared pages.

## Turning it on

```env
DIGITALVIEW_ASSET_FILE=D:\umd\BaseAssetImportTemplate_En.xlsx
DIGITALVIEW_INSTANCE=DigitalView
```

Restart, and the assets load. The file is re-checked every poll interval and
re-read **only when it changes**, so refreshing the inventory means exporting a
new workbook over the old path — nothing else to do.

## Inventory is not monitoring

This is the important part, and the UI says it out loud.

The export tells you what exists and how big it is. It never tells you whether
anything is running. So every Huawei host is stored with **`unknown`** status,
and the platform is labelled **inventory** wherever hosts are counted.

That means Huawei assets:

- **do not** count toward "Total Active Servers" or any availability figure
- **do** appear in Hosts, Capacity, the platform breakdown and Shared
- **do** contribute their CPU / memory / disk totals to capacity numbers

Marking them `up` would have inflated every health number on the site with
machines nothing is actually watching — the same mistake the Dynatrace
discovered-host count already taught us to avoid.

## What gets imported

| Sheet | What it contributes |
|---|---|
| **VM Operating System** | 58 VMs — name, IP, OS, CPU cores, memory |
| **PM Operating System** | 92 physical hosts — same, plus rack position |
| **Rack Server** | 88 servers — BMC IP, model, serial, rack slot |
| **Storage Device** | 6 Dorado arrays |
| **Disk Information** | per-host partitions, summed into total disk GB |
| **VLAN Information** | every IP each asset holds |
| **Component Information** | which application component runs on each host |
| **IP Information** | the application unit (AG, BCS, BTS…) used as the host group |

Grouping is by **application unit** rather than site, because that is what an
operator thinks in. Rack servers have no application unit, so they fall back to
their site.

## Credentials

The workbook carries account columns — `Account name`, `Password`, SNMP
`Authentication Password`, `Encryption Password`. **None of them is read.** The
column names are on a refusal list, so even a future change that asks for one by
name gets an empty string back, and a test asserts that no credential reaches an
imported record.

The file is still sensitive: it holds account names, SNMP users, serial numbers
and the rack position of every machine. `.gitignore` keeps
`BaseAssetImportTemplate*.xlsx` out of the repository — keep it that way.

## The useful side effect

Because Huawei assets carry their real IPs, the **Shared** page now compares the
asset inventory against the monitoring tools automatically. A Huawei asset whose
IP also appears in Zabbix is monitored; one that appears only under Huawei is in
the estate and **nobody is watching it**.

That is a coverage gap report you did not have before, and it comes free with
the import.

## Adding another sheet

Sheet geometry is fixed in `app/huawei_assets.py`: Huawei's template puts five
rows of metadata above the real headers on row 6, a row of internal field ids on
row 7, and assets from row 8. Detail sheets are shallower — headers on row 3,
data from row 5. Both shapes are handled by `_rows()`.

To pull in a sheet that is currently skipped, add it to `_DEVICE_SHEETS` with
its header row. To read a new column, add its header name to the `_pick(...)`
call in `_host_record()` — and if it is a credential column, don't.
