<p align="center">
  <img src="https://raw.githubusercontent.com/917Dhj/dkitchen/main/docs/assets/dkitchen.png" width="240" alt="两个像素风机器人厨师：大的按响服务铃，小的端着锅应声而来">
</p>

<h1 align="center">dkitchen</h1>

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

<p align="center">
  <b>你决定做什么菜，agent 们把厨房转起来。</b> 🧑‍🍳
</p>

<p align="center">
  <a href="https://pypi.org/project/dkitchen/"><img src="https://img.shields.io/pypi/v/dkitchen" alt="PyPI"></a>
  <a href="https://pypi.org/project/dkitchen/"><img src="https://img.shields.io/pypi/pyversions/dkitchen" alt="Python"></a>
  <a href="https://github.com/917Dhj/dkitchen/actions/workflows/tests.yml"><img src="https://github.com/917Dhj/dkitchen/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/917Dhj/dkitchen/blob/main/LICENSE"><img src="https://img.shields.io/github/license/917Dhj/dkitchen" alt="License"></a>
  <a href="https://x.com/dahuoji09"><img src="https://img.shields.io/badge/follow-%40dahuoji09-000000?logo=x" alt="X: @dahuoji09"></a>
</p>

dkitchen 是我每天用来跑科研仓库的多 agent 工作流。它给一组 Claude Code 和 Codex 会话分好岗位，约定它们怎么通过 GitHub issue 交接工作、什么时候必须停下来问你，并配齐把这一切串起来的工具。只有你能做的决定留给你：研究什么、试什么、花多少。

## 🍽️ 为什么是厨房

AI agent 让执行变得很便宜，瓶颈于是落到了你身上。

只开一个 agent，每一步都得你盯着。开好几个，你就成了传话筒：在会话之间来回复制上下文，查任务跑完没有，记住哪个会话在做哪件事，一整天都在批小事。上一个实验早就跑完了，没人发现，GPU 就这么空着。到了晚上，什么都碰过了，却什么都没想。

米其林餐厅的后厨解决的是同一个问题。主厨定菜单、尝味道；其他人各守一个岗位，每道菜都是一张单子，菜做好了就敲一下铃。主厨不切洋葱，也从不对厨房里的事失去掌控。

dkitchen 就这样组织你的 agent。你是主厨。

## 🧑‍🍳 厨房里的岗位

<p align="center">
  <img src="https://raw.githubusercontent.com/917Dhj/dkitchen/main/docs/assets/kitchen-zh.gif" width="100%" alt="像素风的厨房：你和总设计讨论方案，总核查检查 ticket，总编排按铃让 Codex 执行者用上空闲 GPU，审查并合并 PR，再把一个需要你决定的事带给你">
</p>

| 岗位 | 在后厨相当于 | 做什么 | 由谁担任 |
| --- | --- | --- | --- |
| 👑 **你** | 主厨 | 定方向，批设计和预算，判断结果对论文意味着什么。 | 你 |
| 🤖 **总编排** | 出菜口的副主厨 | 排票、排 GPU 和并发；转达消息，审 PR 并合并，向你汇报事实。 | Claude Code · Claude Opus 5.5 |
| 🤔 **总设计** | 研发菜谱的厨师 | 写票，设计方法和所有模型会看到的文字，解读结果。 | Claude Code · Claude Opus 5.5 |
| 🔍 **核查** | 试菜的人 | 花钱之前对照代码和数据核对事实。只报告查到了什么，不做决定。 | Claude Code · Claude Sonnet 5.5 |
| 🔪 **执行者** | 各灶台的厨师 | 一张票一个新会话，各自一个工作树。实现、运行，并在票里汇报。 | Codex · GPT-6.1 Sol |

每个岗位都有成文的规则：自己负责什么、交给谁、什么时候必须停下。哪个岗位都不归的事，agent 会来问你。

## 🎫 一张票怎么走完整个厨房

1. **你和总设计商量好要做什么。** 总设计把它写成一个 GitHub issue：目标、具体步骤，以及这项工作可以花的预算。
2. **核查核对票面的前提**：字段名、数据范围、调用数、费用估算，都对照代码和数据看一遍。
3. **总编排派发这张票**，开一个新的执行者会话，挑空闲的 GPU。
4. **执行者干活，并在票里汇报。** 每条留言开头都是同样的四行：状态、在等谁、关键数字、下一步。长任务用 `ding launch` 启动，任务退出时 `ding` 唤醒执行者，让它看输出、接着干。
5. **结果交给总设计解读。** 总编排审 PR、合并，并记下做了什么、依据是什么。
6. **你读一份简短的汇报**，做下一个决定。

每个决定、申请和结果都留在票里。任何一个会话，或者一周后的你，都能从 GitHub 上把来龙去脉接上。

## ✋ 留给你的事

规则写明了哪些决定属于主厨：

- 研究方向、数据集怎么用、论文里怎么说；
- 超出票内已批预算的模型运行、付费 API 调用和启动服务。

在这些事上，agent 只提建议，然后等你。批准只认你自己的原话，总编排和总设计都不能替你批。

其余的事厨房自己处理，判断标准只有一条：**换谁来做结果都一样的事，agent 直接做；有几种合理做法、选哪种会影响结果、花费或方向的，停下来等你。**

正因为这样，你才能走开。你上课、睡觉或者读论文的时候，一个 issue 监视脚本会在这些时候唤醒总编排：执行者做完一个阶段、卡住或者长时间没动静，你留了言，或者有新 PR。总编排推进已经批准的事，把需要你拍板的事列成清单，等你回来。

## ⚖️ 一个统一的资源池

一个科研仓库手里的资源，比任何单个 agent 看得到的都多：几台服务器上的 GPU、共享的模型服务、CPU 时间、API 预算、token，还有一天里的时间。在 dkitchen 里，总编排用你手上最强的模型，把它们当成一个池子统一调度。

- 🖥️ **算力。** 在空闲的 GPU 之间排票；加并发之前先看共享模型服务的负载。
- 💰 **钱。** 每张要跑模型的票都带一份批好的预算，包括调用数、费用和 GPU 卡·时。预算内的阶段随时可以开始，超出的部分回到你这里。
- 🪙 **Token。** 一张票一个会话，每个会话的上下文都很小；核对事实交给更便宜的模型。
- ⏱️ **时间。** 任务一退出就唤醒负责它的会话，不用等谁想起来去看。

## 🧰 厨具

### 🛎️ `ding`，出菜铃

`ding` 在 agent 会话之间传话，并在长任务结束时唤醒对应的会话。

```bash
ding board                                            # 这个仓库里谁在做什么
ding send '#42' --from orchestrator --message-file note.md
ding launch train -- python train.py                  # 在 tmux 里跑，退出时唤醒我
```

指定会话可以用 UUID、会话名，或者 GitHub issue，比如 `'#42'`；`ding` 会找到认领这张票的会话。在 Linux 上，`ding board` 还会列出没用 `ding` 启动的长时间进程，方便你发现那些跑完了也没人会被唤醒的任务。

`ding` 通过 Codex App Server 和 Codex 会话通信。完整用法见 [docs/ding.md](https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md)（英文）。

### 📋 厨房的日常流程

各岗位每天要做的固定动作也做成了 `ding` 命令，每次都按同样稳妥的方式执行：

- `ding relay` 给票的执行者发消息，确认送达后才在票里留记录。
- `ding merge` 只在审过的那个 HEAD 上合并 PR。
- `ding watch` 在总编排后台等着，票需要处理时唤醒它。
- `ding log` 记下你不在时总编排做了什么。
- `ding check-refs` 核对核查引用的每个文件、行号和摘要。

### 📜 厨房规矩

每个岗位遵守的规则在 [kitchen/](https://github.com/917Dhj/dkitchen/blob/main/kitchen/README.md)（英文）：谁负责什么、票怎么写、什么时候停下来问你，以及运行预算、模型提示词、正式产物登记、无人值守这几个可选模块。

## 🚀 开始使用

### 环境要求

- Linux 或其他 POSIX 系统，装有 `tmux`
- Python 3.10 及以上
- 一个 GitHub 仓库和 `gh` 命令行
- Claude Code，以及在同一用户下运行着 App Server 的 Codex

### 1. 安装 `ding`

```bash
pipx install dkitchen
```

### 2. 把安装 skill 加进 Claude Code

```bash
claude plugin marketplace add 917Dhj/dkitchen
claude plugin install dkitchen@dkitchen
```

### 3. 搭你自己的厨房

在你的仓库里打开 Claude Code，运行：

```text
/setup-dkitchen
```

这个 skill 会先读你的仓库，了解你在做什么、已经有什么：agent 说明文件、测试、GPU、模型调用、长任务。然后一个话题一个话题地问你：用什么语言、要哪些岗位、哪些决定必须留给你、要哪些可选模块、有哪些算力。它会把打算写入的每一条规则、每个脚本和每项 GitHub 改动先拿给你看，你同意后才写。之后想改设置，随时再运行一次。

## ❓ 常见问题

**让 agent 无人值守地跑，安全吗？**
凡是会改变方向、结果或花费的事，它们都会停下；批准只认你在票里的原话。每个动作都是 GitHub 上的一条留言或一个 PR，第二天早上喝咖啡的时候就能把昨晚的事查一遍。

**适合我的仓库吗？**
这些规则是为计算类科研写的，其中大部分也适用于任何有长任务的仓库。安装 skill 会按你的仓库改写这些规则。

**必须同时用 Claude Code 和 Codex 吗？**
目前是的，这是我每天在用的组合。

**小项目用这么多流程，会不会太重？**
先只开总编排和执行者，需要时再加总设计和核查。

## 💡 关于

*d* 是我的记号。dkitchen 是我管自己厨房的方式，写下来，好让你也能开起自己的厨房。它诞生在我的科研仓库里：这个仓库在最初的四周里，用这套工作流处理了 171 个 issue，合并了 111 个 PR。

米其林后厨这个比喻来自 Lauren Tan，网名 [Poteto](https://github.com/poteto)。这套工作流的很多做法都受到了 Poteto 管理 agent 方式的启发。用 GitHub issue 管票的做法，最初改自 [Matt Pocock 的 skills](https://github.com/mattpocock/skills) 里的 issue tracker 配置。谢谢两位。🙏

## 许可证

[MIT](LICENSE)
