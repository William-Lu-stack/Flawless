# 数据库 Adapter 接入

本目录由数据库平台/DBA 团队维护。一个数据库产品一个模块，统一输出 `database` 域资源。

最低证据合同见 `spec.py`：连通性、实例角色、会话、慢 SQL、锁等待、复制、容量、备份和最近变更。恢复不能以“SQL 已提交”为完成，至少重新验证对应故障指标和业务探针。

推荐模块：`mysql.py`、`postgresql.py`、`oracle.py`、`oceanbase.py`、`gaussdb.py`、`tidb.py`、`dameng.py`。凭据只通过 Secret 引用，禁止进入资源事实、证据和异常文本。
