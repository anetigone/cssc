请遵守以下规则：
- Lean smoke / 真实 checker：需要时直接申请非沙箱运行，尤其是 elan toolchain 相关命令。
- 不读取 `.env` 内容，只检查存在性和用脚本消费它，避免把 key 打出来。