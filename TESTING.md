# 测试记录

本文件记录实际运行过的验证，而不是声称的能力。环境为容器内的 Linux、Python 3.11、
SQLite + fakeredis 作为本地替身（生产运行使用 PostgreSQL 16 + Redis 7）。

## 自动化检查

运行全部三项（需 `fakeredis`，仅在本地测试替身中使用）：

```bash
cd backend
python3 tests/test_erasure_domain.py
python3 tests/test_erasure_api.py
python3 tests/test_erasure_races.py
```

1. **领域级清除检查（41 项，全部通过）** — `backend/tests/test_erasure_domain.py`
   - 归一化：`P-1`、`p-1 `、`Ｐ-１` 视为同一纸样编号；`P-1` 不会误伤 `P-10`。
   - 范围：同编号在多个批次中的分析都被纳入；影响登记、分析、导出次数计数正确。
   - 读停：记号落地后，`runs.get` / `runs.export` 立即返回 410，即使清理尚未完成。
   - 清理：冻结快照、参数、结果、错误、SHA256 清空；共享产物删除；登记行删除；
     复核删除；审计明细中的原值替换为清除标记；其他纸样登记与分析不受影响。
   - 凭据：`receipt_sha256` 为 64 位；凭据 JSON 不含任何测量数值。
   - 终态：全部位置 `verified` 后才置 `completed`；读取/导出/复核持续拒绝。
   - 复活：队列中的旧任务重跑不会写回结果或产物；同编号重录被 409 拒绝（含变体）。
   - 晚到计算：在 `calculate` 执行中途完成清除，worker 写回时二次校验发现记号，
     放弃结果并删除产物。
   - 中断恢复：在产物步骤抛错后，清除停留在 `failed`、`completed_at` 为空；
     `resume_pending` 重跑并通过逐处核验后才完成。

2. **API 级检查（18 项，全部通过）** — `backend/tests/test_erasure_api.py`
   - `POST /erasures` 仅 admin 可调用（analyst/viewer 403），同步返回范围与进度。
   - `GET /erasures`、`GET /erasures/{id}` 对所有角色可用，展示步骤与凭据。
   - 清除后 `GET /runs/{id}`、`GET /runs/{id}/export`、`POST /runs/{id}/review` 均为 410。
   - `/overview` 中被清除运行只返回空快照；来源登记不再显示该编号。
   - 同编号重录（含大小写/空格变体）409；其他纸样可创建并完成新分析。
   - 新分析不会把已撤回编号写入冻结快照。

3. **并发与晚到检查（6 项，全部通过）** — `backend/tests/test_erasure_races.py`
   - 与清除范围扫描并发的冻结分析仍会被纳入并清除（共享锁/排他锁排序）。
   - 计算过程中完成清除：结果被丢弃，产物不写入磁盘。
   - 清除前已开始的读取能完成且看到旧值；清除完成后的读取返回 410。
     因此“旧读取晚到”不会在清除确认后再输出数值。

4. **增量 schema 升级** — 用只含旧版 `runs` 表的 SQLite 库启动，
   `src/config/migrations.py` 补出 `specimen_keys`/`erased`/`erasure_id` 三列，
   旧行与旧值保持原样，新表由 `create_all` 建立。

5. **前端模块** — `node --check` 与 ES module 动态导入对所有新增/修改页面通过
   （`pages/erasure.js`、`pages/analysis.js`、`pages/overview.js`、`router/index.js`
   等未引用未定义符号）。

## 未覆盖 / 需在完整编排中复验

- 真正的 PostgreSQL 行锁/咨询锁、Redis 7 真实队列、多容器网络未在本沙箱运行；
  逻辑使用 `pg_advisory_xact_lock` 与共享锁，SQLite 下退化为进程内读写锁。
- 浏览器真实交互（点击清除、进度条刷新）未做端到端浏览器测试，仅做模块导入与语法检查。
- 备份卷、已下载到用户设备的 CSV 不在应用可控范围，见 README“已知边界”。

## 复现

```bash
# 完整编排
cp .env.example .env && docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
# 打开 http://localhost:18242 → 授权撤回，输入 DEMO-1-0001 观察范围与逐处核验
```

本地无 Docker 时，测试脚本使用 `DATABASE_URL=sqlite:///...`、`REDIS_URL` 指向
fakeredis 的 TCP 服务，并设置 `ARTIFACT_DIR` 指向临时目录。SQLite 下清除锁退化为
进程内读写锁；生产环境请以 PostgreSQL 的共享/排他咨询锁为准。
