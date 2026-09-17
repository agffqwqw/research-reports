# 本机 Worker —— 研报生成流水线

> 职责：**轮询服务器任务队列 → 抓取巨潮公告 → 抽取全文 → 交由 AI 做六维度评判 → 回传结果**

---

## 为什么需要本机 Worker

服务器（2核2G）不跑 AI，只做接单、存库、展示；**生成在本机完成**：

```
用户在网站提交股票代码
        ↓
【服务器】写入任务队列（pending）
        ↓
【本机 Worker】轮询 → 拉取任务（processing）
        ↓
【本机】抓巨潮公告 → 下载 PDF → 抽取全文
        ↓
【本机 · AI】按技能包六维度框架评判 → 生成 report.json
        ↓
【本机 Worker】回传服务器
        ↓
【服务器】写库 + 扣次数 → 网站可查
```

---

## 配置

```bash
cp worker.env.example worker.env   # 然后填 WORKER_TOKEN
```

`WORKER_TOKEN` 必须与 `backend/app.env` 里的一致。若留空，脚本会自动从 `../backend/app.env` 读取。

---

## 命令

| 命令 | 作用 |
|:---|:---|
| `python worker.py check` | **只读**查看队列（不会改变任务状态） |
| `python worker.py claim` | 拉取队首任务（状态改为 processing），输出 JSON |
| `python worker.py fetch <code> <period>` | 抓公告 → 下载 → 抽取全文，输出关键词页码索引 |
| `python worker.py show <code> <period> "<pages>"` | 按页查看全文，如 `"5,6,7"` 或 `"9-12"` |
| `python worker.py submit <task_id> <report.json>` | 回传成功（**扣次数**） |
| `python worker.py fail <task_id> "<原因>"` | 回传失败（**不扣次数**） |

### 通知队列（服务器入队 → 本机发飞书）

| 命令 | 作用 |
|:---|:---|
| `python worker.py notice peek` | **只读**查看待发通知 |
| `python worker.py notice pull` | 取出一条（输出 JSON：id / kind / title / body / link） |
| `python worker.py notice done <id>` | 标记已发送 |
| `python worker.py notice fail <id> "<原因>"` | 标记失败（**自动重试，上限 3 次**） |

> **为什么通知要绕一圈回本机发**：服务器上的后端无法调用飞书 ——
> 飞书是 WorkBuddy 的 MCP 工具，只在 WorkBuddy 运行时可用，服务器上只是个普通 Python 进程。
> 所以改成「服务器只记录，本机来发」：
>
> ```
> 后端需要通知 → 写进 notification 表（pending）
>                      ↓
> 本机 WorkBuddy 定时任务 → notice pull → 飞书发送 → notice done
> ```
>
> 好处：服务器不需要 SMTP / 飞书凭证；换渠道只改本机侧；发失败会留在队列里可重试。

### 权限批准（代管理员执行）

| 命令 | 作用 |
|:---|:---|
| `python worker.py perm list` | 列出待批准的权限申请 |
| `python worker.py perm approve <token>` | 按 token 批准 |
| `python worker.py perm approve --id <id>` | 按申请 id 批准（更常用） |

> **为什么需要这条路**：邮件 / 飞书里的批准链接指向服务器地址
> （`APP_BASE_URL`），**服务尚未部署时链接打不开**。
> 本机 Worker 持有受信令牌，可以直接执行批准 ——
> 于是「在 WorkBuddy 对话里说一句『批准权限申请』」就能完成，完全不依赖链接可达性。
>
> 部署上线后链接自然也能用，**两条路并存**。

---

## 单次任务的完整操作序列

```bash
# 1) 查看队列
python worker.py check

# 2) 拉取任务
python worker.py claim
# → {"id": 3, "company_code": "600519", "company_name": "贵州茅台", ...}

# 3) 抓取 + 抽取（约 20–40 秒）
python worker.py fetch 600519 2026H1
# → 输出 source_url / disclosure_date / 关键词页码索引

# 4) 按索引读关键页（供六维度评判）
python worker.py show 600519 2026H1 "5,6,7"

# 5) 生成 report.json（严格遵循技能包 references/report-json-schema.md）
#    并用技能包的校验脚本自检：
python "C:/Users/user/.workbuddy/skills/cninfo-report-deep-dive/scripts/validate_report_json.py" report.json

# 6) 回传
python worker.py submit 3 <report.json 路径>
```

**失败时**（抓不到公告、报告格式异常等）：

```bash
python worker.py fail 3 "未找到 2026 年半年度报告正文"
```

---

## 六维度取数地图（评判时用）

| 要找什么 | 搜索关键词 | 备注 |
|:---|:---|:---|
| 核心财务指标 | `主要会计数据和财务指标` | 半年报约 p5–7 |
| 费用与变动原因 | `财务报表相关科目变动分析表` | **含公司自述的变动原因，价值最高** |
| 分产品/分地区 | `占比10%以上的产品或服务` | 含收入、成本、毛利率 |
| 行业景气 | `行业情况` / `行业分析` | MD&A 第一项 |
| 产销量 | `产量` / `销量` | **制造业有，银行/证券无** |
| 资产负债明细 | `资产构成重大变动` | 借款、货币资金在这 |
| 三表原文 | `合并资产负债表` / `合并现金流量表` | 单位多为**千元或元，注意换算** |
| 风险 | `公司面临的风险` | 公司自认风险 |
| 分红 | `利润分配` | 半年报常为「不分配」 |

---

## 常见坑（实践中踩过的）

1. **公告标题必须用「结尾匹配」** —— 用「包含」会抓到《关于 2026 年半年度报告披露的**提示性公告**》（只有 2 页），而不是正文。
2. **报告单位不统一** —— 比亚迪/宁德时代是**千元**，茅台/中信是**元**。换算亿元时分别 ÷100,000 和 ÷100,000,000。
3. **不以销量的行业** —— 银行、证券没有「销量」「毛利率」口径，这两项只能标「不适用」。
4. **产销快报只适用于整车企业** —— 电池、白酒、光模块公司都不披露月度产销。
5. **`check` 必须只读** —— 早期版本误用了 `next` 接口，导致「查看队列」把任务消费掉了（已修复，改用 `peek`）。
6. **失败的生成不扣次数** —— 所以拿不准时宁可 `fail`，不要勉强 `submit`。

---

## 自动化（可选）

已配置两个 WorkBuddy 定时任务：

| 任务 | 频率 | 作用 |
|:---|:---|:---|
| 研报站点·处理生成队列 | 每小时 | 自动完成上述生成流程 1–6 步 |
| 研报站点·推送飞书通知 | 每小时 | 拉取通知队列 → 用飞书推送给管理员 |

也可以随时手动对本机 AI 说「处理队列」/「推送通知」来即时触发。

### 本机调用 lark-cli 的注意点

本机 bash 缺 coreutils（`sed`/`dirname`/`uname` 都没有），**lark-cli 的包装脚本跑不起来**，
必须绕过它用 node 直接调用：

```bash
cd C:\Users\user\.workbuddy\binaries\node\cli-connector-packages
"C:\Users\user\.workbuddy\binaries\node\versions\22.22.2-3\node.exe" \
    node_modules/@larksuite/cli/scripts/run.js im +messages-send \
    --as bot --user-id ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx --markdown $'...'
```

> ⚠️ **发送必须用 `--as bot`**：user 身份缺 `im:message.send_as_user` scope，
> 用 `--as user` 会被拒。bot 身份可用。
