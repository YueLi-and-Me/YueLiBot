# 用户手册

月璃是一台要装在你电脑或服务器上的程序，不是网页服务：装好之后她跑在你的机器里，
数据也在你自己的硬盘上。这份手册带你从零装到能用，全程大概二十分钟。

## 先选一条路

三条路线装的是同一套后端，区别只在**要不要桌宠**、**跑在哪台机器**。

<div class="grid cards" markdown>

-   :material-microsoft-windows:{ .lg .middle } **Windows 桌面版**

    ---

    带桌宠的完整形态：立绘站在桌面角落，会动、会说话，托盘常驻。

    [:octicons-arrow-right-24: 走这条路](#windows-桌面版)

-   :material-server:{ .lg .middle } **服务器部署**

    ---

    没有图形界面，只跑 QQ 与管理面板。适合长期开机、随时找得到她。

    [:octicons-arrow-right-24: 走这条路](#服务器部署)

-   :material-robot-happy:{ .lg .middle } **把安装交给 AI**

    ---

    把这一页发给你在用的 AI，由它查环境、装依赖、改配置。

    [安装规格](#把安装交给-ai)

</div>

## 不知道自己该选哪条

**这台电脑平时开着，你想要桌宠陪你。** 走 Windows 桌面版。要求 Windows 10 或 11，
装完她会出现在屏幕角落，托盘里能开关。

**你想让她 24 小时在线，手机随时能找到。** 走服务器部署。可以是一台云服务器、
一台旧笔记本或家里的 NAS，Linux 和 Windows Server 都行；管理面板用浏览器打开。

**你只有一台 Windows，也不想让她一直占着屏幕。** 还是走 Windows 桌面版，
装完后在设置里关掉桌宠外壳，她就退成后台服务，不影响你干别的。

**你完全不想碰命令行。** 把安装交给 AI：把[那一页](ai-install.md)发给 Claude、GPT
或任意能执行命令的 AI，它照着规格做，你只管回答它的问题。

## Windows 桌面版

1. [确认环境要求](deployment/requirements.md)——五分钟，看你的机器够不够
2. [下载与安装](deployment/install.md)——拿到代码、装依赖
3. [第一次启动与配置](deployment/first-run.md)——同意协议、填一个 API Key
4. [桌面桌宠](deployment/windows.md)——让她出现在屏幕上
5. [接入 QQ](adapters/index.md)——想让她在群里说话，再做这一步

## 服务器部署

1. [确认环境要求](deployment/requirements.md)——服务器不需要图形环境
2. [下载与安装](deployment/install.md)——只装后端，前端产物可以不构建
3. [无头部署](deployment/headless.md)——systemd 常驻、开机自启、面板怎么访问
4. [接入 QQ](adapters/index.md)——协议端与月璃在同一台机器上跑

## 把安装交给 AI

[这一页](ai-install.md)按「先问什么、不许做什么、做到什么算完、按什么顺序、
错了怎么判」写成了规格。你把它发给 AI 就行，规格里同时写明了完成判据，
最后验收时你照着对一遍即可。

## 装完之后

- [配置](configuration/index.md)——名字、人格、模型、功能开关分别在哪份文件里
- [功能](features/index.md)——记忆、表情包、主动搭话、日程与睡眠
- [管理面板](webui/index.md)——浏览器里的观察与配置入口
- [常见问题](troubleshooting.md)——按症状查：启动、面板、模型、QQ、桌宠、数据
