"""报表数据源适配器。

**这一层只做一件事：把外部格式变成原始行。** 不认识数据库主键、不写 Mongo、
不做任何字段映射 —— 映射是归一化那一步的事，而 `raw_reports` 存的必须是**未经
解释的原始事实**（理由见 `db/mongo.py` 的模块 docstring）。

MVP 只有文件导入（[设计文档第四节](../../../docs/design/2026-08-19-mvp-design.md)
说明了为什么不先接 Ads API）。平台 API 适配器接进来时，协议要长什么样见
[`base.py`](base.py) 里 `ReportProvider` 的 docstring —— 那里写了为什么现在**不**
预先把两种形态统一成一个签名。
"""
