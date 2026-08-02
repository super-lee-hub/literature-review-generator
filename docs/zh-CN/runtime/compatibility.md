# 当前产物边界

生产运行时只有一套 clean-cut 契约。只有当前 typed settings、当前 job
workspace 和当前已注册 artifact 可以进入运行。旧输入必须以明确诊断
fail-closed；不会被投影成新的 readiness，也不会被静默升级。

## 允许的输入

- `config.ini` 必须通过当前 typed settings schema 校验。
- job 必须具备 source inventory、readiness policy、append-only attempt
  history、Registry identity 和当前 stage terminals。
- Outline Intelligence v3 是唯一的 outline 生产路径。evidence views、
  corpus ledger、review intent、coverage contract、relation map、candidate
  plan、node DAG、receipts 和 adoption record 都必须是已注册产物。
- Review、citation、validation、repair、export 和 attestation 只消费当前
  versioned contracts，并校验 Registry 依赖与 hash。

## 旧输入的处理

- 旧配置 section、旧 workspace 投影和未注册报告文件直接 fail-closed。
- Markdown outline 或人类可读报告不能满足 outline、review、validation、
  readiness 或 completion gate。
- 运行时不提供迁移命令、旧 CLI、外部 stage handler 或把旧产物转换成当前
  产物的 adapter。
- 缺少 identity、dependency、receipt、terminal 或 content hash 证据时，只
  生成 quarantine 诊断，并保持 `canonical_ready=false`。

## 必须审计的状态变化

显式 summary reuse、歧义 identity 决策、outline adoption、repair apply、
force delete 和 quarantine release 都必须写入不可变 audit record，记录
actor、reason、scope、输入 hash、policy snapshot 和 artifact 标识。不支持
长期布尔绕过开关。

## 路径与依赖规则

- spec、config、summary 的相对路径分别从所属文件所在目录解析。
- 跨 job 依赖使用 `external_job`，以 `job_id`、`artifact_id`、`content_hash`
  作为身份；path 只是定位投影。
- 父 artifact 存在未失效的 child dependency 时不得删除；force delete 必须
  同时写审计并使受影响 child 失效。

## 可选集成边界

Live API、Playwright、heavy OCR 测试属于 optional marker，必须显式启用并
满足前置条件。strict-offline 测试禁止外部网络，仅允许 loopback，并把离线
边界传递给 Python 子进程。
