# -*- coding: utf-8 -*-
"""admin/ — 管理面板与后台签到（HTTP 面 + 定时任务）。

与 routes/ 同一套约定：register(app, core) 里 core 传 server 模块，引擎件
（_cors / _current_creds / _refresh_models* / SUPPORTED_MODELS / MODEL_DETAILS /
logger / benefit / session）一律在**请求期**经 core.xxx 现查，导入期不绑定。
这样 tests/test_panel_overview.py 对 server 上这些名字的打桩替换对 admin 层
同样生效——包括对 benefit.credit_activities 一类上游函数的打桩。

拆出来不为别的：server.py 原本把这 237 行和签名/上游发送混在一起，改一处
面板字段要在一个 1368 行的文件里上下翻。现在面板契约只在这一层。
"""
