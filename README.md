# DSH 插件库

[DeepSeek Harness](https://github.com/deepseek-ai) 的插件合集。一个目录一个插件，互不依赖，可以只装其中一个。

| 插件 | 做什么 | 外部依赖 |
| --- | --- | --- |
| [dsh-session-progress](./dsh-session-progress) | 会话表头的进度栏：当前步骤、进展条、本回合用时 | 无 |

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
