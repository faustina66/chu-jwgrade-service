# 日常使用与维护

这份文档讲**装好之后**的事：怎么看状态、怎么改频率、改了教务密码怎么办、
怎么停、怎么换服务器。

第一次部署看 [`deployment.md`](deployment.md)。

下面的命令都在服务器上以 root 执行。服务跑在 `/opt/jwgrade`，
systemd 单元名 `jwgrade`，以专用的 `jwgrade` 用户身份运行。

> **本页只有 `--once` 会真的访问教务系统**，其余命令一律不联网，随便跑。

---

## 速查

| 想知道 / 想做 | 命令 |
|---|---|
| 还活着吗 | `systemctl is-active jwgrade` |
| 详细状态 | `systemctl status jwgrade --no-pager -l` |
| 最近在干嘛 | `journalctl -u jwgrade -n 30 --no-pager` |
| 实时跟日志 | `journalctl -u jwgrade -f` |
| 今天的登录额度 | `cat /opt/jwgrade/data/login_rate.json` |
| 看变更历史 | 见下方「查状态 · 第三层」 |
| 停掉（连开机自启） | `systemctl disable --now jwgrade` |
| 开回来 | `systemctl enable --now jwgrade` |

---

## 一、查状态：三层，从外往里

### 第一层：活没活

```bash
systemctl status jwgrade --no-pager -l
```

要看的是这四行，不是那个绿点：

| 字段 | 正常 | 不对劲 |
|---|---|---|
| `Active` | `active (running)` + 一个长的 since | `activating (auto-restart)` = 在崩溃重启循环里 |
| `Memory` | 当前值 ≈ peak | peak 一直往上抬 = 内存泄漏 |
| `CPU` | 几小时才用掉一两秒 | 持续占用 = 卡在某个循环里 |
| `Tasks` | 1 | 多出来的是没回收的子进程 |

**如果是 `inactive (dead)` 而且退出码是 20 或 21，那是故意没重启的。**
20 = 登录失败，21 = 配置错误。服务单元里配了
`RestartPreventExitStatus=20 21` 就为这个 —— 这两种情况重启一万次也一样，
必须人去处理。**这时候不要 `restart`。**

只要一个字的答案：

```bash
systemctl is-active jwgrade
```

### 第二层：最近在干嘛

```bash
journalctl -u jwgrade -n 30 --no-pager
```

只挑轮询结果，一天的情况几秒钟扫完：

```bash
journalctl -u jwgrade --since today --no-pager | grep -E "无变化|检测到|首次运行|下次"
```

正常长这样，一轮三行：

```text
21:24:23 INFO  src.adapters.chd | 从 ...historyCourseGrade.action 解析出 N 条成绩
21:24:23 INFO  jw | 无变化（共 N 门课程）
21:24:23 INFO  jw | 下次检查：724 秒后（常规档）
```

**间隔是跳的，这是对的。** 常规档基准 30 分钟，实际会在 27–33 分钟之间跳。
抖动是故意加的，免得请求固定落在整点那种全校都在刷的时刻。

想验证有没有漏轮：把「下次检查 N 秒后」加到当前时间戳上，
和下一条日志对一下，应该分毫不差。

> **日志里没有登录行，才是最好的状态。**
> 那说明全程在复用已有会话，既没换票也没提交密码。

### 第三层：抓到了什么

```bash
cd /opt/jwgrade && sudo -u jwgrade .venv/bin/python -m src.main --history 20
```

**`cd` 不能省。** `python -m src.main` 是按当前目录找 `src` 这个包的，
在别的目录跑会报 `No module named 'src'`。这对所有 `-m src.main` 的命令都成立。

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
时间戳是 epoch 秒，肉眼读不出来，要换算 —— 很容易把昨天的记录当成今天的。

---

## 二、状态文件

没有数据库。状态就是 `/opt/jwgrade/data/` 下的八个普通文件：

| 文件 | 装的东西 |
|---|---|
| `grades.json` | 主快照 —— 所有课程现在是什么样，比对只用它 |
| `history.jsonl` | 变更历史，只追加，每行一条 |
| `pending.json` | 发件箱 —— 推送失败待补发的通知 |
| `session.json` | 登录会话 Cookie。**搬机时它跟着走 = 零认证** |
| `login_rate.json` | 限速账本 |
| `login_blocked` | 登录阻断标记 |
| `last_push.marker` | 心跳计时（空文件，靠修改时间记事） |
| `jwgrade.lock` | 单实例运行锁（内核持有，进程死了自动释放） |

快照答「现在是什么」，历史答「什么时候变成这样的」，两者互补。

### 有没有文件被封存过

读坏的状态文件**不会被删**，而是改名保留成
`<原名>.corrupt-<时间戳>-<随机后缀>`，下一轮当它不存在、照常继续。

所以**损坏是静默自愈的** —— 不告警、不停机，你只会在日志里看到两行。

```bash
ls -l /opt/jwgrade/data/*.corrupt-* 2>/dev/null || echo "没有封存文件，干净"
```

有输出意味着：那一轮之后**快照被重建成了新基线**。期间如果出了分，
那条通知就漏掉了 —— 而且它也进不了 `history.jsonl`，因为压根没被检测到。
好在封存的原文件还在，拿它和现在的 `grades.json` 比一比就知道漏了什么。

不过真正会触发它的基本只有「人工编辑快照编坏了」：断电、写一半、磁盘写满
都被原子写兜住了（临时文件写成功才替换，中途出错原件一个字节没动）。

**所以要记住的其实是一句操作纪律：手动改过 `grades.json` 之后，
一定回读一次验证，别改完就走。**

---

## 三、改轮询频率

`schedule` 段是**热重载**的：改完保存，最多等一轮就生效，**不用重启**。
只有这一段有这待遇 —— `notify` / `safety` / `storage` 的改动要重启。

```bash
nano /opt/jwgrade/config.yaml
```

三档不是按时间表切的，是按**距上次抓到变化多久**：

| 档 | 什么时候在这一档 |
|---|---|
| 加速 15 分钟 | 距上次变化 < `active_duration_minutes`（默认 2 小时） |
| 常规 30 分钟 | 中间地带 |
| 省电 60 分钟 | 距上次变化 ≥ `idle_after_hours`（默认 24 小时） |

**程序启动本身不算「有动静」**，所以重启不会白白进加速档 —— 这是故意的，
不然每次维护都要多打教务系统几轮。

### 改完先看「每天多少轮」

秒数没有画面感，轮数有。改完跑一下（不联网、不需要凭据）：

```bash
cd /opt/jwgrade && sudo -u jwgrade .venv/bin/python -m src.main --preflight --config /opt/jwgrade/config.yaml
```

它会把节奏翻译成人话：

```text
轮询节奏：加速 15 分钟 / 常规 30 分钟 / 省电 60 分钟
静默时段：01:00–07:00 不查
每天约 36 轮（常规档）／72 轮（加速档）
```

### 硬规则一：三个间隔有下限

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

公平地说，**轮询不等于登录** —— 会话能复用，绝大多数轮次只是带 Cookie 发一个
GET，登录闸是独立的、照样拦得住。但每天上千次请求对一个学校系统仍然是很大的量，
而它是学校的，不是你的。10 分钟已经相当积极了。

### 硬规则二：顺序必须是 加速 ≤ 常规 ≤ 省电

**这个坑真的有人踩过**：把常规档改成 5 分钟，加速档留着默认的 15 分钟没动。
结果是一出分反而**从 5 分钟慢到 15 分钟**，程序做的事和字面意思正好相反。

这类配置最难自己发现：不报错、能跑、日志一切正常，只是通知来得比平时晚，
而你根本不会去怀疑配置。所以现在它**直接拒绝启动**：

```text
schedule.active_interval_seconds 是 900 秒（加速档），比常规档的 300 秒还慢。
三档的意思是「越可能出分越查得勤」，顺序必须是 加速 ≤ 常规 ≤ 省电——
照现在这样，一出分反而会变慢，正好和设计相反
```

三个细节：

- **相等是允许的**（`加速 = 常规` 只是把加速档关掉，不算配错）
- **只改一个也会被拦。** 缺的键按默认值补齐再比 —— 只动 `interval_seconds`
  是最自然的改法，那时加速档还是默认的 900 秒，照样是反的
- `adaptive: false` 时不检查，因为另外两档根本不参与计算

改坏了不会停摆：YAML 解析失败会记一条警告、**继续用旧配置**。

### 命令行 `--interval` 走同一道校验

`--interval` 是调试用的临时覆盖，它会顺带把 `adaptive` 关掉。
它和配置文件那条路共用同一套下限和警告线，不存在「命令行能绕过」的口子。

---

## 四、改了教务密码怎么办

**主动跑一遍安装脚本，别等它自己撞。**

```bash
systemctl disable --now jwgrade
cd /opt/jwgrade && bash deploy/setup.sh
```

先停掉服务是为了让脚本的冒烟测试能独占运行锁；成功后脚本会自动把服务
重新启用并启动。

在这一步选 `y`：

```text
凭据文件 /etc/jwgrade.env 已存在。
要重新输入教务密码吗？（在教务系统改过密码就选 y）[y/N]: y
教务系统密码: ********
已更新密码，PushPlus token 保持不变。
```

剩下的它自己做：清掉上次失败留下的阻断标记 → 冒烟测试（打错还能再试两次）
→ 重启服务。**PushPlus token 不用重敲。**

如果冒烟测试失败，服务会保持停用状态。先按终端报错处理，不要立刻手动 `restart`。

### 它不会立刻报错，这才是麻烦的地方

改完密码之后程序还能正常跑一阵子：

| 阶段 | 用什么 | 受影响吗 |
|---|---|---|
| 当下 | 已经建立的会话 Cookie | ❌ 照常抓成绩 |
| 会话过期后 | 换票（长期票据） | 多半也失效了 |
| 票据也没了 | **提交密码** ← 这里才撞上 | ✅ 被驳回 |

所以可能过几小时、也可能过几天才停 —— **而且大概率挑一个你没在看的时候**。

撞上那一刻：只撞**一次**就停（这是阻断标记的作用），推一条
「⚠️ 教务监控已停止」到微信，退出码 20，systemd 不再拉起。
不会反复撞、不会把账号试锁死。

**万一忘了，收到告警之后也是同一条路** —— 一样重跑 `setup.sh` 选 `y`，
阻断标记它会自己清。

### 如果提示的是验证码或账号锁定

**那不是密码错误，不要为了解锁去改密码。**
先去 <https://ids.chd.edu.cn> 手动通过验证码、或等待锁定自动解除，
然后重新执行上面的流程，输入**原来的密码**即可。

---

## 五、停、开、卸载

### `disable` 不等于 `stop`

这两条命令是**正交**的：

| 命令 | 管的是 |
|---|---|
| `start` / `stop` | 现在这个进程跑不跑 |
| `enable` / `disable` | 以后开机自己起不起 |

只跑 `systemctl disable jwgrade` 会看到一句
`Removed .../multi-user.target.wants/jwgrade.service`，
**看着像停了，其实进程还在跑** —— 这是最容易误判的一种状态。

一条命令两件事一起办：

```bash
systemctl disable --now jwgrade
```

`--now` 的意思就是「顺便立刻执行对应动作」。反过来恢复：

```bash
systemctl enable --now jwgrade
```

验一下，应该是 `inactive` 和 `disabled` 两行：

```bash
systemctl is-active jwgrade; systemctl is-enabled jwgrade
```

### 停下来安全吗

安全，三点：

1. **时机上几乎不会撞车。** 程序绝大部分时间在两轮之间睡觉，
   `stop` 发的信号基本都落在睡眠里。
2. **万一撞在抓取中间**，最坏是「推送已发出、快照还没落盘」，
   下次起来重复推一条。这是设计时选的方向 —— **可能重复，绝不丢失**。
3. **不留需要手工清理的东西。** 运行锁是内核持有的，进程一死自动释放；
   状态全在 `data/` 的磁盘文件里，停多久都不丢。

再启动时**不会立刻提交密码**：先试恢复已有会话（零认证），
会话过期才换票（仍然不用密码），再不行才走密码。
所以短时间停机几乎是零成本的。

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

要搬的**只有 `data/` 目录**，其它全部重装。

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
重建基线、**那期间出的分永远检测不出来**。这是不可逆的 —— 成绩页上只有
「现在是什么」，没有「什么时候变的」。

**顺带一个好处：** `session.json` 跟着走的话，新机器第一轮直接复用旧会话，
**一次认证都不用**。

> **服务器到期释放是不可逆的。** 买之前就在日历上设好到期提醒。

---

## 七、什么时候才真要重启

**默认答案是「不用」。** 对着这张表确认之后再动手：

| 改了什么 | 要重启吗 | 为什么 |
|---|---|---|
| README / 文档 | ❌ 连同步都不用 | 服务根本不读这些文件 |
| 校验逻辑、注释、报错文案 | ❌ **不急** | 只在加载配置时跑；下次因为别的原因重启时自然生效 |
| 抓取 / 比对 / 渲染 / 推送的代码 | ✅ 要 | 跑起来会用到，不重启新代码不生效 |
| `config.yaml` 的 `schedule` 段 | ❌ | 热重载，最多等一轮 |
| `config.yaml` 的 `notify` / `safety` / `storage` 段 | ✅ 要 | 热重载只覆盖 `schedule` |
| `/etc/jwgrade.env`（密码、token） | ✅ 要 | systemd 只在启动时读它 |

**为什么要这么小心：**

**会话还活着时重启是免费的**（恢复 Cookie，零认证）；
**会话一旦过期，重启就等于一次认证** —— 而频繁认证正是会把账号刷到
「频繁登录」被冻结的那种模式。

问题在于**你事先不知道会话死没死**。所以默认别重启。

> 「同步了代码」不等于「必须立刻重启」。新代码躺在磁盘上不会坏，
> 下次重启时自然生效；而每一次重启都是在赌会话还活着。

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

## 八、手动跑命令：凭据的坑

`sudo -u jwgrade .venv/bin/python -m src.main --demo` 会报
「enabled 是 true 但没有 token」。

原因是 **`sudo -u` 不会加载 `/etc/jwgrade.env`**，而 `JW_PASSWORD` 和
`PUSHPLUS_TOKEN` 都在那里面。这个坑很容易让人以为推送配错了。

分界线：

| 子命令 | 要凭据吗 | 怎么跑 |
|---|---|---|
| `--history` `--preflight` `--unlock-login` | 不要 | `sudo -u jwgrade` 就行 |
| `--demo` `--test-notify` `--report` | **要**（只调推送，不碰教务系统） | 见下面 |
| `--once` | **要**，而且**会真的访问学校** | 同下，慎用 |

需要凭据时，让 systemd 去读环境文件：

```bash
sudo systemd-run --quiet --wait --collect --pipe --unit=jwgrade-demo-$$ --uid=jwgrade --gid=jwgrade --working-directory=/opt/jwgrade --property="EnvironmentFile=/etc/jwgrade.env" --property="ReadWritePaths=/opt/jwgrade/data" /opt/jwgrade/.venv/bin/python -m src.main --demo --config /opt/jwgrade/config.yaml
```

**别图省事写成 `export PUSHPLUS_TOKEN=...`** —— 那样凭据会进命令行参数，
同机器上任何人 `ps` 一下就能看到。

想测推送通道的话，用现成的脚本更省事：

```bash
cd /opt/jwgrade && bash tools/test_push.sh
```

---

## 九、命令一览

| 参数 | 做什么 | 碰教务系统吗 | 要凭据吗 |
|---|---|---|---|
| `--preflight` | 检查配置和状态目录，翻译轮询节奏 | ❌ | ❌ |
| `--history [N]` | 看成绩变更历史，可只看最近 N 条 | ❌ | ❌ |
| `--unlock-login` | 确认已换正确密码后解除登录阻断 | ❌ | ❌ |
| `--demo` | 用假数据模拟一次出分通知，看当前详略级别的实际效果 | ❌ | ✅ |
| `--test-notify` | 只发一条测试推送 | ❌ | ✅ |
| `--report` | 把当前快照里的完整成绩单推到微信（读快照，服务停着也能用） | ❌ | ✅ |
| `--once` | **真的查一次**就退出 | ✅ | ✅ |
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

| 现象 | 多半是 |
|---|---|
| 服务 `active` 但微信一直没消息 | 正常。**没有变化就不推送**，出分才推 |
| 日志里 `首次运行，已建立基线…本次不推送` | 正常，首次只记基线，不会把历史成绩刷屏 |
| `inactive (dead)`，退出码 20 | 登录被拒。看日志上一条，多半是改过密码 —— 走第四节 |
| `inactive (dead)`，退出码 21 | 配置有问题，报错里写了是哪一条 |
| 微信收不到，但日志说推送成功 | **八成是没关注 PushPlus 的公众号** |
| 通知比平时晚很多 | 检查三档顺序，见第三节「硬规则二」 |
| 凌晨出的分早上才收到 | 正常，静默时段默认是 01:00–07:00 |

**连续失败 5 次**会推一条告警给你。
**7 天没发过任何东西**会主动报一次平安 —— 所以**没收到平安信，
才是该去看一眼的信号**。
