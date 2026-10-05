# pack.sh 冒号陷阱修复报告 · 2026-10-06

> 触发：与「重启实例」对话同步时核对 `rebuild-drill-20260930.md` 的遗留待办，
> 其中 **P0「修复 `pack.sh` 行尾校验误报」** 需确认是否真的修好。
> 结论：**P0 已修好；但修完之后暴露出了一个此前从未被发现的阻塞级新缺陷。**
>
> 实测环境：Windows 11 + PortableGit bash（MSYS2）
> 验证方式：本机**真实执行** `bash deploy/pack.sh`（非静态审查）

---

## 一、结论先行

| 项 | 状态 |
|:---|:---|
| `rebuild-drill-20260930.md` 的 **P0（`grep` 行尾校验误报）** | ✅ **已修复**（改用 `cmp` 字节级比对） |
| `rebuild-drill-20260930.md` 的 P1（`deploy.sh` 补跑 backup） | ✅ 已修 |
| ③ 级演练「重启自启」实测 | ✅ 已补测（4 服务 `enabled`） |
| 🔴 **新发现（阻塞级）：`pack.sh` 打包步骤本身失败** | ✅ **本次已修复** |
| 🔴 **新发现：同一陷阱在脚本内共 3 处** | ✅ 本次已全部修复 |
| 🟢 **`pack.sh` 在这台机器上首次全程跑通** | ✅ 实测通过 |

---

## 二、新缺陷：`tar -f` 遇到 Windows 绝对路径，把盘符当主机名

### 现象

```
xx 打包失败（tar 退出码 2）
```

只有这一行。因为原脚本把 tar 的 stderr 重定向进了 `2>/dev/null`，
**真正的报错被自己吞掉了**。

### 根因（去掉 `2>/dev/null` 后才看到）

```
tar (child): Cannot connect to C: resolve failed
tar: C\:/Users/user/.../deploy/dist-upload/probe.tar.gz: Cannot write: Broken pipe
tar: Child returned status 128
tar: Error is not recoverable: exiting now
rc=2
```

| 环节 | 说明 |
|:---|:---|
| **GNU tar 的 `-f` 支持 `host:path` 远程语法** | 这是给 rsh 时代的远程归档用的 |
| `OUT` 是 `C:/Users/.../x.tar.gz` | 冒号前的 **`C`** 被解析为**远程主机名** → 尝试连接主机 `C` → 失败 |
| 结果 | 文件**根本没生成**，但报错被吞，只剩「退出码 2」 |

> ⚠️ **迷惑点（为什么一直没被发现）**：
> 1. 手动敲**相对路径**的同样命令 → 正常，于是被当成"偶发"；
> 2. 09-30 那次是**手动复刻 tar 规则绕过**脚本的（演练报告原文：「只能手动复刻 tar 规则绕过」）
>    → **副作用：脚本的缺陷被成功掩盖了**；
> 3. 旧版 GNU tar 无此行为，新版默认解析 `host:path` —— 属于「环境升级引入的回归」。

### 影响面

**从 09-30 直到 2026-10-06，`pack.sh` 在这台机器上从未跑通过。**
任何依赖它的部署流程（含重建演练）都必须人工绕过 —— 而人工绕过的步骤**不在脚本里**，
下一个重建者不会知道。

### 修法

两处独立生效的改动（**双保险**，任一个单独都够）：

```diff
- tar -czf "$OUT" --exclude="$OUT" ...
+ tar --force-local -czf "$OUT_REL" ...
```

| 手段 | 作用 |
|:---|:---|
| `OUT_REL="deploy/dist-upload/..."` | 用**相对路径**，根本没有冒号 → 不触发解析 |
| `--force-local` | 显式告诉 tar「`-f` 是本地文件，别解析 host:path」 |

> 📌 **对 `--exclude` 的影响**：`--exclude='deploy/dist-upload'` 是**相对**模式，
> 本来就只匹配包内路径；原写法 `--exclude="$OUT"`（绝对路径）**永远匹配不上**，
> 属于无效排除项（真正防住"包中包"的是那条相对排除）。本次一并去掉，注释已更正。

### 同一陷阱的另外 2 处（一并修复）

打包成功后，**读取**包的两条命令也挂了同样的错：

```
tar (child): Cannot connect to C: resolve failed
gzip: stdin: unexpected end of file
```

| 位置 | 原写法 | 后果 |
|:---|:---|:---|
| 凭证扫描 | `tar -tzf "$OUT" \| grep ...` | grep 拿到**空输入** → 无匹配 → 打印「✓ 未包含任何凭证文件」 |
| 目录清单 | `TOC="$(tar -tzf "$OUT")"` | `TOC` 为空 → 每个目录都判「✗ 缺失」 |

> 🔴 **这里有个极其危险的点**：凭证扫描失败时**打印的是 ✅**。
> 也就是「安全检查静默放行」—— 一个永远说"合格"的检查。
> 这与 09-30 报告里那句教训**完全同构**：
> **「一个永远说'不合格'的检查，和一个永远说'合格'的检查，危害是等价的。」**
> 上次踩的是"永远说不合格"（误报拦死），这次是它的镜像。

---

## 三、修复验证（真实执行，非静态审查）

```
==> 项目根目录: <工作区>/research-reports
==> 构建前端                                     ✓ built in 5.12s（dist 193K）
==> 统一脚本行尾为 LF                            ✓ 无 CR（字节级比对校验）
==> 打包
==> 已生成: .../deploy/dist-upload/research-reports-20261006-0452.tar.gz
    体积: 236K
==> 检查包内是否误含凭证（应当为空）              ✓ 未包含任何凭证文件
==> 包内依据目录（应含 reports 与 frontend/dist）
    ✓ backend   ✓ frontend/dist   ✓ worker   ✓ deploy   ✓ reports
```

**`pack.sh` 在本机的首次全程通过。**

---

## 四、给 `offsite-backup.py` 加的防回归注释

排查时特地确认了 Python 侧**没有**同类问题：

| 实现 | 行为 |
|:---|:---|
| `tarfile.open(dest, "w:gz")`（`offsite-backup.py:65`） | ✅ **正常** —— Python 直接开文件，不走 shell，无冒号解析 |
| `subprocess.run(["openssl", ...])` | ✅ 参数以**列表**传递，不经过 shell |
| `subprocess.run(["systemctl", "is-active", unit])`（`healthcheck.py:113`） | ✅ 同上 |

已在 `offsite-backup.py` 打包函数处加注释，**明确禁止**将来有人把 `tarfile` 改成 GNU tar 命令行。

---

## 五、一句话

> **③ 级的 P0 确实修好了；但修好之后，脚本才第一次真正跑到"打包"那一步 —— 于是撞上了此前从未暴露的 Windows 冒号陷阱：`tar -f` 把盘符 `C` 当成了远程主机名。**
>
> **它同时具备两个最难发现的特征：报错被 `2>/dev/null` 吞掉、而安全检查失败时打印的是 ✅。**
