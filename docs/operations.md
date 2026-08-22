# 日常使用与维护

这份文档介绍部署完成后的常用操作，包括查看状态、调整频率、更新教务密码、
停止服务和迁移服务器。

第一次部署看 [`deployment.md`](deployment.md)。

下面的命令都在服务器上以 root 执行。服务安装在 `/opt/jwgrade`，
systemd 单元名 `jwgrade`，以专用的 `jwgrade` 用户身份运行。

> 查看状态、日志、历史记录和执行预检不会访问教务系统。`--once`、启动或重启
> 监控服务可能访问教务系统，并在已有会话失效时触发认证。

---

## 速查

| 想知道 / 想做 | 命令 |
|---|---|
| 是否正在运行 | `systemctl is-active jwgrade` |
| 详细状态 | `systemctl status jwgrade --no-pager -l` |
| 查看最近日志 | `journalctl -u jwgrade -n 30 --no-pager` |
| 实时查看日志 | `journalctl -u jwgrade -f` |
| 今天的登录额度 | `cat /opt/jwgrade/data/login_rate.json` |
| 看变更历史 | 见下方「查状态 · 第三层」 |
| 停止服务并关闭开机自启 | `systemctl disable --now jwgrade` |
| 启动服务并开启开机自启 | `systemctl enable --now jwgrade` |

---

## 一、查看运行状态

### 第一层：确认服务是否运行

```bash
systemctl status jwgrade --no-pager -l
```

重点查看下面四项：

| 字段 | 正常状态 | 异常迹象 |
|---|---|---|
| `Active` | `active (running)` + 一个长的 since | `activating (auto-restart)` = 在崩溃重启循环里 |
| `Memory` | 当前值 ≈ peak | peak 一直往上抬 = 内存泄漏 |
| `CPU` | 几小时才用掉一两秒 | 持续占用 = 卡在某个循环里 |
| `Tasks` | 1 | 多出来的是没回收的子进程 |

**如果是 `inactive (dead)` 而且退出码是 20 或 21，那是故意没重启的。**
20 = 登录失败，21 = 配置错误。服务单元里配了
`RestartPreventExitStatus=20 21` 会阻止 systemd 自动重启。此时必须先处理登录或配置问题，
不要直接执行 `restart`。

只想快速确认是否正在运行，可以执行：

```bash
systemctl is-active jwgrade
```

### 第二层：查看最近日志

```bash
journalctl -u jwgrade -n 30 --no-pager
```

只查看当天的轮询结果：

```bash
journalctl -u jwgrade --since today --no-pager | grep -E "无变化|检测到|首次运行|下次"
```

正常情况下，每轮会出现类似下面的三行日志：

```text
21:24:23 INFO  src.adapters.chd | 从 ...historyCourseGrade.action 解析出 N 条成绩
21:24:23 INFO  jw | 无变化（共 N 门课程）
21:24:23 INFO  jw | 下次检查：724 秒后（常规档）
```

轮询间隔会有小幅变化，这是正常现象。常规档基准为 30 分钟，实际间隔约为
27–33 分钟。随机抖动可以避免请求总是集中在固定时刻。

如需检查轮询是否连续，可根据「下次检查 N 秒后」计算预计时间，
再与下一条轮询日志的时间进行比较。

> **日志里没有登录行，才是最好的状态。**
> 那说明全程在复用已有会话，既没换票也没提交密码。

### 第三层：查看成绩变更记录

```bash
cd /opt/jwgrade && sudo -u jwgrade .venv/bin/python -m src.main --history 20
```

**必须先执行 `cd /opt/jwgrade`。** `python -m src.main` 会从当前目录查找 `src` 包；
如果在其他目录运行，将提示 `No module named 'src'`。所有 `-m src.main` 命令都遵循这一规则。

上次成功抓取是什么时候，看 `grades.json` 的修改时间：

```bash
ls -l /opt/jwgrade/data/
```

今天的登录额度用掉多少：

```bash
cat /opt/jwgrade/data/login_rate.json
```

里面是 `[{"t": <epoch 秒>, "k": "ticket"|"password"}]`。
**只有 `k` 是 `password` 的那些算日额度**；`ticket` 归小时额度管。
时间字段采用 Unix 时间戳，需要换算后才能确认具体日期和时间。

---

## 二、状态文件

没有数据库。状态就是 `/opt/jwgrade/data/` 下的八个普通文件：

| 文件 | 用途 |
|---|---|
| `grades.json` | 当前成绩快照，成绩变化比对以它为基准 |
| `history.jsonl` | 变更历史，只追加，每行一条 |
| `pending.json` | 推送失败后等待补发的通知 |
| `session.json` | 登录会话 Cookie；迁移服务器后会优先尝试恢复该会话 |
| `login_rate.json` | 限速账本 |
| `login_blocked` | 登录阻断标记 |
| `last_push.marker` | 心跳计时（空文件，靠修改时间记事） |
| `jwgrade.lock` | 单实例运行锁（内核持有，进程死了自动释放） |

快照保存当前成绩状态，历史文件记录成绩何时发生变化，两者用途不同且相互补充。

### 有没有文件被封存过

读坏的状态文件**不会被删**，而是改名保留成
`<原名>.corrupt-<时间戳>-<随机后缀>`，下一轮当它不存在、照常继续。

程序会自动封存损坏文件并继续运行，相关处理记录会写入日志。

```bash
ls -l /opt/jwgrade/data/*.corrupt-* 2>/dev/null || echo "没有封存文件，干净"
```

有输出意味着：那一轮之后**快照被重建成了新基线**。期间如果出了分，
该变化可能无法发送通知，也不会写入 `history.jsonl`，因为程序没有检测到它。
好在封存的原文件还在，拿它和现在的 `grades.json` 比一比就知道漏了什么。

这种情况通常由人工修改快照时破坏文件格式引起。程序采用原子写入：只有临时文件
完整写入后才会替换原文件，因此断电、写入中断等情况通常不会破坏原快照。

手动修改 `grades.json` 后，必须重新读取并验证文件内容和 JSON 格式。

---

## 三、改轮询频率

`schedule` 段是**热重载**的：改完保存，最多等一轮就生效，**不用重启**。
只有这一段支持热重载；修改 `notify`、`safety` 或 `storage` 后需要重启服务。

```bash
nano /opt/jwgrade/config.yaml
```

三个档位不是按固定时刻切换，而是根据**距离上次检测到成绩变化的时长**决定：

| 档 | 什么时候在这一档 |
|---|---|
| 加速 15 分钟 | 距上次变化 < `active_duration_minutes`（默认 2 小时） |
| 常规 30 分钟 | 中间地带 |
| 省电 60 分钟 | 距上次变化 ≥ `idle_after_hours`（默认 24 小时） |

程序启动本身不算成绩变化，因此重启不会自动进入加速档，
避免维护操作增加不必要的查询请求。

### 修改后先确认每天预计查询次数

修改后可运行以下预检命令查看预计查询次数。该命令不会联网，也不需要凭据：

```bash
cd /opt/jwgrade && sudo -u jwgrade .venv/bin/python -m src.main --preflight --config /opt/jwgrade/config.yaml
```

它会用分钟数和每天预计轮数显示当前轮询设置：

```text
轮询节奏：加速 15 分钟 / 常规 30 分钟 / 省电 60 分钟
静默时段：01:00–07:00 不查
每天约 36 轮（常规档）／72 轮（加速档）
```

### 规则一：三个间隔均有下限

**低于 300 秒直接拒绝启动**，低于 900 秒记一条警告。
换算表（静默 1–7 点，一天 18 小时在跑）：

| 间隔 | 每天轮数 |
|---|---|
| 60 分钟 | 18 |
| **30 分钟（默认）** | **36** |
| 15 分钟 | 72 |
| 10 分钟 | 108 |
| 5 分钟 | 216 |
| 1 分钟 | 1080 —— **会被拒绝** |

轮询不等于登录。会话有效时，大多数轮次只是携带 Cookie 获取成绩，认证频率限制也会
继续生效。不过，每天上千次请求仍会给学校系统带来不必要的压力，建议不要把间隔设置得
短于 10 分钟。

### 规则二：顺序必须满足“加速 ≤ 常规 ≤ 省电”

例如，把常规档改成 5 分钟，却保留默认的 15 分钟加速档，
就会导致出分后检查间隔从 5 分钟增加到 15 分钟，与加速档的含义相反。

这类错误不容易通过运行状态发现：程序可以正常启动，日志也可能没有明显异常，
但通知会比预期更晚。因此，程序会在启动时直接拒绝此类配置：

```text
schedule.active_interval_seconds 是 900 秒（加速档），比常规档的 300 秒还慢。
三档的意思是「越可能出分越查得勤」，顺序必须是 加速 ≤ 常规 ≤ 省电——
按照当前设置，检测到成绩变化后反而会降低检查频率
```

三个细节：

- **相等是允许的**（`加速 = 常规` 只是把加速档关掉，不算配错）
- **只修改一个值也会进行校验。** 缺失的配置项会按默认值补齐后再比较；如果只修改
  `interval_seconds`，仍需确认它与默认的 900 秒加速档保持正确顺序
- `adaptive: false` 时不检查，因为另外两档根本不参与计算

如果热重载时 YAML 解析失败，程序会记录警告并**继续使用上一份有效配置**。

### 命令行 `--interval` 走同一道校验

`--interval` 是调试用的临时覆盖，它会顺带把 `adaptive` 关掉。
它和配置文件那条路共用同一套下限和警告线，不存在「命令行能绕过」的口子。

---

## 四、改了教务密码怎么办

修改教务密码后，应主动重新运行部署脚本，无需等待旧会话失效。

```bash
systemctl disable --now jwgrade
cd /opt/jwgrade && bash deploy/setup.sh
```

先停止服务，是为了让部署检查独占运行锁；检查成功后，脚本会自动重新启用并启动服务。

在这一步选 `y`：

```text
凭据文件 /etc/jwgrade.env 已存在。
要重新输入教务密码吗？（在教务系统改过密码就选 y）[y/N]: y
教务系统密码: ********
已更新密码，PushPlus token 保持不变。
```

随后脚本会自动清除上次失败留下的阻断标记、执行运行检查，并重新启动服务。
如果密码输入错误，还可以再尝试两次；**无需重新输入 PushPlus token。**

如果部署检查失败，服务会保持停用状态。请先根据终端提示处理问题，不要立即手动执行 `restart`。

### 为什么不会立即发现密码已经失效

修改密码后，程序可能仍会继续运行一段时间：

| 阶段 | 用什么 | 受影响吗 |
|---|---|---|
| 当下 | 已经建立的会话 Cookie | ❌ 照常抓成绩 |
| 会话过期后 | 换票（长期票据） | 多半也失效了 |
| 票据也已失效 | 提交密码 | ✅ 被认证服务器拒绝 |

因此，服务可能在几小时或几天后、旧会话失效时才停止运行。

认证被拒绝后，程序只尝试一次便会停止，并向微信推送
「⚠️ 教务监控已停止」。程序随后以退出码 20 结束，systemd 不会再次启动它，
从而避免重复提交错误密码导致账号被锁定。

如果在收到停止告警后才发现密码已修改，同样重新运行 `setup.sh` 并选择 `y`；
部署脚本会自动清除阻断标记。

### 如果提示的是验证码或账号锁定

**那不是密码错误，不要为了解锁去改密码。**
先去 <https://ids.chd.edu.cn> 手动通过验证码、或等待锁定自动解除，
然后重新执行上面的流程，输入**原来的密码**即可。

---

## 五、停止、启动与卸载

### `disable` 不等于 `stop`

这两项设置相互独立：

| 命令 | 管的是 |
|---|---|
| `start` / `stop` | 当前是否运行服务 |
| `enable` / `disable` | 是否设置为开机自动启动 |

只跑 `systemctl disable jwgrade` 会看到一句
`Removed .../multi-user.target.wants/jwgrade.service`，
这只会取消开机自启，当前进程仍会继续运行。

同时停止服务并关闭开机自启：

```bash
systemctl disable --now jwgrade
```

`--now` 的意思就是「顺便立刻执行对应动作」。反过来恢复：

```bash
systemctl enable --now jwgrade
```

验证结果应分别为 `inactive` 和 `disabled`：

```bash
systemctl is-active jwgrade; systemctl is-enabled jwgrade
```

### 停下来安全吗

安全，三点：

1. 程序大部分时间都在等待下一轮检查，停止服务通常不会中断正在进行的请求。
2. 如果停止操作恰好发生在抓取过程中，最坏的情况是「推送已经发出，但快照尚未写入」，
   下次启动后可能重复推送一次。程序优先避免通知丢失，因此允许极少量重复推送。
3. **不留需要手工清理的东西。** 运行锁是内核持有的，进程一死自动释放；
   状态保存在 `data/` 目录中，停止服务不会删除这些文件。

再次启动时，程序会先尝试恢复已有会话；会话过期后再尝试换票，
只有前两种方式都失败时才会提交密码。

### 彻底卸载

```bash
systemctl disable --now jwgrade
rm -f /etc/systemd/system/jwgrade.service
systemctl daemon-reload
rm -f /etc/jwgrade.env
userdel jwgrade
rm -rf /opt/jwgrade
```

**`/opt/jwgrade/data/` 里是你的成绩快照和历史，删了就没了。**
想留着就先把这个目录拷走。

---

## 六、换服务器

迁移时需要保留的运行数据集中在 **`data/` 目录**，其他组件可在新服务器上重新安装。

```bash
# 在旧机器上
tar czf jwgrade-data.tar.gz -C /opt/jwgrade data

# 传到新机器后，先跑一遍全新部署，然后停掉服务、覆盖 data/
systemctl disable --now jwgrade
tar xzf jwgrade-data.tar.gz -C /opt/jwgrade
chown -R jwgrade:jwgrade /opt/jwgrade/data
systemctl enable --now jwgrade
```

**为什么必须搬：** `data/grades.json` 丢了的话，新机器会当成首次运行、
重建基线，**迁移期间发生的成绩变化将无法被识别**。这是不可逆的，因为成绩页只显示
「现在是什么」，没有「什么时候变的」。

如果一并迁移 `session.json`，新服务器会优先尝试恢复旧会话；
旧会话仍有效时无需重新认证，失效时会按正常流程换票或登录。

> **服务器到期释放是不可逆的。** 买之前就在日历上设好到期提醒。

---

## 七、哪些修改需要重启

大多数日常查看操作不需要重启。修改后可根据下表判断：

| 改了什么 | 要重启吗 | 为什么 |
|---|---|---|
| README / 文档 | ❌ | 服务不读取这些文件；需要查看最新版文档时再更新代码 |
| 校验逻辑、注释、报错文案 | ❌ 可稍后处理 | 只在加载配置时生效；可在下次计划重启时一并启用 |
| 抓取 / 比对 / 渲染 / 推送的代码 | ✅ 要 | 跑起来会用到，不重启新代码不生效 |
| `config.yaml` 的 `schedule` 段 | ❌ | 热重载，最多等一轮 |
| `config.yaml` 的 `notify` / `safety` / `storage` 段 | ✅ 要 | 热重载只覆盖 `schedule` |
| `/etc/jwgrade.env`（密码、token） | ✅ 要 | systemd 只在启动时读它 |

**为什么要避免不必要的重启：**

会话仍有效时，重启通常可以直接恢复 Cookie，无需重新认证；
会话失效时，重启可能触发换票或密码登录。频繁认证可能触发学校的安全策略。

由于无法提前确认会话是否仍然有效，默认不要进行不必要的重启。

> 更新代码后不一定需要立即重启。仅修改文档时无需重启；修改运行代码后，
> 可在合适的时间重启服务，使新代码生效。

### 升级到新版本

```bash
systemctl disable --now jwgrade
cd /opt/jwgrade && git pull
.venv/bin/pip install -r requirements.lock.txt
sudo -u jwgrade .venv/bin/python -m src.main --preflight --config /opt/jwgrade/config.yaml
systemctl enable --now jwgrade
```

`config.yaml` 和 `data/` 都在 `.gitignore` 里，`git pull` 不会动它们。
但**升级前还是先把 `data/` 拷一份**，代价只是几百 KB。

如果新版本加了配置项，`config.example.yaml` 里会有；
`--preflight` 会告诉你缺什么。

---

## 八、运行需要凭据的命令

`sudo -u jwgrade .venv/bin/python -m src.main --demo` 会报
「enabled 是 true 但没有 token」。

原因是 **`sudo -u` 不会加载 `/etc/jwgrade.env`**，而 `JW_PASSWORD` 和
`PUSHPLUS_TOKEN` 都保存在该文件中。因此，直接使用 `sudo -u` 运行需要推送凭据的命令时，
会出现缺少 token 的提示。

分界线：

| 子命令 | 要凭据吗 | 怎么跑 |
|---|---|---|
| `--history` `--preflight` `--unlock-login` | 不要 | `sudo -u jwgrade` 就行 |
| `--demo` `--test-notify` `--report` | **需要**（只调用推送服务，不访问教务系统） | 见下方说明 |
| `--once` | **要**，而且**会真的访问学校** | 同下，慎用 |

需要凭据时，让 systemd 去读环境文件：

```bash
sudo systemd-run --quiet --wait --collect --pipe --unit=jwgrade-demo-$$ --uid=jwgrade --gid=jwgrade --working-directory=/opt/jwgrade --property="EnvironmentFile=/etc/jwgrade.env" --property="ReadWritePaths=/opt/jwgrade/data" /opt/jwgrade/.venv/bin/python -m src.main --demo --config /opt/jwgrade/config.yaml
```

不要直接执行 `export PUSHPLUS_TOKEN=...`。这种写法可能让凭据出现在命令记录或
进程环境中，应继续使用受权限保护的 `/etc/jwgrade.env`。

测试推送通道时，建议使用仓库自带的脚本：

```bash
cd /opt/jwgrade && bash tools/test_push.sh
```

---

## 九、命令一览

| 参数 | 用途 | 是否访问教务系统 | 是否需要凭据 |
|---|---|---|---|
| `--preflight` | 检查配置和状态目录，翻译轮询节奏 | ❌ | ❌ |
| `--history [N]` | 看成绩变更历史，可只看最近 N 条 | ❌ | ❌ |
| `--unlock-login` | 确认已换正确密码后解除登录阻断 | ❌ | ❌ |
| `--demo` | 使用虚构数据模拟一次成绩通知，查看当前详略级别的显示效果 | ❌ | ✅ |
| `--test-notify` | 只发一条测试推送 | ❌ | ✅ |
| `--report` | 把当前快照里的完整成绩单推到微信（读快照，服务停着也能用） | ❌ | ✅ |
| `--once` | **实际查询一次成绩**后退出 | ✅ | ✅ |
| `--interval N` | 固定轮询间隔（秒），覆盖配置并关闭自适应 | ✅ | ✅ |
| `--dump` | 保存成绩页原始 HTML，排查解析问题用 | ✅ | ✅ |
| `--set-password` / `--clear-password` | 把密码存进系统密钥链 / 从密钥链删除 | ❌ | ❌ |
| `--config PATH` | 指定配置文件，默认 `config.yaml` | — | — |

> `--dump` **不要当常规步骤**：存下来的 HTML 里有你的姓名、学号和全部成绩，
> 看完记得删。
>
> 服务器上的标准做法是 `setup.sh` 写的 `/etc/jwgrade.env`。
> `--set-password` 走的是系统密钥链，无头 Linux 上通常没有可用后端，
> 那种情况下程序会自动退回环境变量。

---

## 十、常见情况

| 现象 | 可能原因或说明 |
|---|---|
| 服务 `active` 但微信一直没消息 | 正常。**没有变化就不推送**，出分才推 |
| 日志里 `首次运行，已建立基线…本次不推送` | 正常，首次只记基线，不会把历史成绩刷屏 |
| `inactive (dead)`，退出码 20 | 登录被拒。看日志上一条，多半是改过密码 —— 走第四节 |
| `inactive (dead)`，退出码 21 | 配置有问题，报错里写了是哪一条 |
| 微信收不到，但日志说推送成功 | 先确认已经关注 PushPlus 公众号，并检查 PushPlus 后台发送记录 |
| 通知比平时晚很多 | 检查三档顺序，见第三节「硬规则二」 |
| 凌晨出的分早上才收到 | 正常，静默时段默认是 01:00–07:00 |

**连续失败 5 次**会推一条告警给你。
**连续 7 天没有发送任何通知**时，程序会主动发送一次心跳消息。如果超过该周期仍未
收到心跳消息，建议检查服务状态和日志。
