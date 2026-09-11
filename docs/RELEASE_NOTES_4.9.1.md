# BLCaptain 调色公式 Skill v4.9.1

## 中文

这次更新把完整目录变成一致的普通用户合同：32 个唯一公式中，31 个支持照片，31 个支持普通 SDR 视频。法式暖调仅照片，夜景黑金仅视频，其余 30 个支持两种媒体。所有声明支持的入口都能直接选择、生成计划并执行；内部研发沿革不再出现在用户清单、方案或 README 中。

新增完整公式图谱：照片 31 张、视频 31 张逐项前后对比，并提供中英文名称、公式 ID、核心视觉作用、证据等级和来源许可。13 张复用了开发过程中许可与哈希可追溯、且由队长明确接受的最终结果；其余 49 张为权利清楚的公开公式演示，只展示方向，不冒充人工审美接受。

安全边界不变：每份素材仍须通过三档预演、黑白位、剪切、综合色彩、肤色与记忆色检查，并确认当前唯一 `plan_id`；原图不会被覆盖，结果组失败时整组回滚。

验证：Python 3.10.18 整库 1061 项通过，52 项按环境条件跳过，0 失败、0 错误；公开包 155 个文件、0 审计阻断，并在全新目录完成安装与真实 `plan → render` 照片闭环。

## English

v4.9.1 gives the complete catalog one consistent user contract: 32 unique formulas, with 31 photo paths and 31 standard-SDR video paths. `french-warm` is photo-only, `night-black-gold` is video-only, and the other 30 support both media types. Every declared path can be selected, planned, and executed; internal development history is no longer exposed in user-facing lists, plans, or documentation.

The new bilingual formula atlas contains 31 photo and 31 video before/after comparisons with names, IDs, visual intent, evidence level, and source license. Thirteen entries reuse rights-cleared, hash-traceable final results explicitly accepted during development. The other 49 are rights-cleared public demonstrations of direction, not claims of human aesthetic approval.

Per-source safety remains unchanged: three-strength preflight, black/white points, clipping, colorfulness, skin and memory-color checks, current `plan_id` confirmation, no source overwrite, and atomic rollback.

Verification: 1061 tests passed on Python 3.10.18, 52 were conditionally skipped, with zero failures or errors. The public package contains 155 files with zero audit blockers and completed a clean-install `plan → render` photo workflow.
