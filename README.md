# DSH 插件库

[DeepSeek Harness](https://github.com/deepseek-ai) 的插件合集。一个目录一个插件，互不依赖，可以只装其中一个。

| 插件 | 做什么 | 外部依赖 |
| --- | --- | --- |
| [dsh-a6-failover](./dsh-a6-failover) | A6 请求故障换商、平台默认与手动智能路由切换（0.6.3） | A6 控制面访问令牌与用户 ID |
| [dsh-session-progress](./dsh-session-progress) | 会话表头的进度栏：当前步骤、进展条、本回合用时 | 无 |

## 配套工具

不是 DSH 插件、装不进 `plugin_manager` 的独立程序放这里：

| 工具 | 做什么 | 运行环境 |
| --- | --- | --- |
| [modelkeeper](./modelkeeper) | 模型管家：拉取中转站 / 官方端点的模型清单并测首字延迟与生成速度，一键写进 WorkBuddy、DeepSeekHarness、Codex、Claude Code 的配置，含备份还原与智能路由 | Python 3.12+（tkinter，读写 YAML 需 PyYAML） |

## 安装

把要用的插件目录放到一个**固定位置**（之后不要删、不要移），然后：

```js
plugin_manager install_bundle target: "<插件目录的绝对路径>"
```

装完刷新 DSH 即可。每个插件目录下都有各自的 README，写着它用到的服务、slot 和注意事项。

> ⚠️ 本地目录安装走的是 pnpm link：装完删掉或移动那个目录，插件会直接失效。

## 目录约定

```
dsh-plugins/
├── <插件名>/
│   ├── package.json        # dsh.bundle.patch + dsh.client 声明
│   ├── cordis.patch.yml    # 往 profile 里插一行 entry
│   ├── icon.svg            # 插件市场图标
│   ├── locale/{zh,en}.json # 市场里的标题与描述
│   ├── lib/{index,client}.js
│   └── README.md
└── ...
```
