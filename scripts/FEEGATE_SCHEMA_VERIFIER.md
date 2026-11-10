# FeeGate 商品 Schema 与说明卡自检器

任务：TASK-20261110-COMM-07。Python 3.10+ 标准库实现，全程离线、只读，不自动修复或改写商品。

## 使用

```bash
python3 scripts/feegate_schema_verifier.py --verify-catalog --verify-docs
python3 scripts/feegate_schema_verifier.py --verify-catalog --catalog-dir data/products
python3 scripts/feegate_schema_verifier.py --verify-docs
python3 scripts/feegate_schema_verifier.py --mock-verify
python3 -m unittest discover -s scripts -p test_feegate_schema_verifier.py
```

缺省递归核验 `data/products/`。`--verify-catalog` 检查数据与类型，`--verify-docs` 检查说明卡；可同时运行。
`--mock-verify`（别名 `--mock`）始终在内存中执行两类检查，不读取或创建物理文件。
返回码：0 为全部通过，1 为资产不合规，2 为目录、读取或扫描完整性错误。CLI 输出 JSON 报告，可由调用方重定向保存。
报告生成时间使用本地时区的 `YYYY-MM-DD HH:MM`；资产时间严格校验格式，时区由产品契约约定。

## 商品目录约定

```text
data/products/
  media_metrics.json
  media_metrics.schema.json
  media_metrics.md
  ledger.jsonl
  ledger.schema.json
  ledger.md
  a-product/
    dataset.json
    dataset.schema.json
    README.md
```

每个 JSON/JSONL 数据文件都需要同名 `.schema.json`。JSONL 的 Schema 逐行应用，每行必须是对象，空行忽略，空账本报错。
每份资产对应同名 `.md`；独立产品子目录可以用该目录的非空 `README.md` 覆盖其中资产。
根目录的通用 README 不会替代所有商品说明卡。缺失、空白或夹杂 HTML 的说明卡均不合规。
HTML/HTM 和 CSV、SQLite 等非 JSON/JSONL 数据文件会报告格式违规；所有说明文件应使用 Markdown。

普通 JSON 商品需要字符串 `product_id`、直读 `updated_at` 和可核验指纹。
含 `records` 的商品要求原生整数 `item_count` 与数组长度一致；布尔值不能冒充整数。
JSONL 每条记录需要 `sha256_hash`。索引对象以 `products` 数组组织条目，条目包含 `product_id`、`artifact`、`item_count`、`updated_at`、`sha256_hash`。

## 强类型 Schema 子集

为避免对未知约束静默放行，本工具实现并严格限制 JSON Schema 2020-12 的以下子集：

| 类型 | 支持的约束 |
| --- | --- |
| 通用 | 显式 `type`、`enum`、`title`、`description`、标准 2020-12 `$schema` 标识 |
| object | `properties`、`required`、强制 `additionalProperties: false` |
| array | 显式 `items`、`minItems`、`maxItems`、`uniqueItems` |
| string | `minLength`、`maxLength`、`pattern`、`format` |
| integer/number | `minimum`、`maximum`，禁止布尔冒充数值 |

只允许一种原生类型，可与 null 形成显式可空类型。禁止未约束对象、缺失元素类型的数组、数组直接嵌套数组以及任意多类型兜底。
字符串格式包括 `human-time`（真实日历时间且严格直读）、`sha256`（64 位小写十六进制）与 `uri`（HTTP/HTTPS）。
不支持 `$ref`、远程 Schema、`anyOf` 等其他关键字；遇到不支持的约束直接失败，不进行网络解析。
Schema 来自受维护的本地契约，正则表达式按本地可信配置处理。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "count": {"type": "integer", "minimum": 0},
    "is_active": {"type": "boolean"},
    "observed_at": {"type": "string", "format": "human-time"},
    "sha256_hash": {"type": "string", "format": "sha256"}
  },
  "required": ["count", "is_active", "observed_at", "sha256_hash"],
  "additionalProperties": false
}
```

此例描述记录类型，商品文件还需声明其身份、更新时间与账本等实际字段；不能把示例直接当作全部商品的通用 Schema。

## 语义与指纹

除 Schema 外，递归检查明确的数字字段、布尔标志、时间字段和 SHA256 字段，避免把这些字段声明成普通字符串规避校验。
拒绝 HTML 键名或数据、字符串 true/false/null、异构无类型数组和嵌套数组；显式 null 仍须通过 Schema 的可空约束。
拒绝重复 JSON 键、NaN/Infinity、特殊文件、符号链接与超限文件。UTF-8 文件可带 BOM，单文件上限 16 MiB。

对象内部的 `sha256_hash` 使用：删除当前对象的该字段，其他内容按键排序，以 UTF-8、无额外空格、保留 Unicode 的规范 JSON 编码后计算 SHA256。
记录内的指纹会分别重算。商品索引条目的 `sha256_hash` 覆盖被引用资产的完整原始 UTF-8 字节（包括 BOM）；索引条目不按对象自指纹处理。
索引还必须与实际商品的 `product_id`、`item_count`、`updated_at` 在值和原生类型上完全一致。
有经核验索引指纹的商品可以不嵌入自指纹；没有任何可核验指纹来源的商品会失败。
引用路径相对索引目录，必须在目录树内，禁止绝对路径、`..`、空路径段、反斜杠。

历史资产没有 Schema、使用旧时间格式或缺少配套说明卡时，真实目录审计会返回非零；测试全绿不代表历史资产已迁移。
本工具不会自动改动已签名、已有消费者依赖的商品字节。

## 覆盖率

```bash
python3 -m trace --count --missing --summary --coverdir /tmp/sprint9-comm-coverage --ignore-dir /usr --module unittest discover -s scripts -p test_feegate_schema_verifier.py
```

以标准库 `trace` 测量核心引擎可执行行覆盖率，门槛为至少 90%，不等同于分支覆盖率。
测试包含内存演练零物理 I/O、磁盘只读、路径越界、符号链接、FIFO、错误 Schema、类型与指纹篡改、逐行账本及说明卡检查。

本次交付 129 项新增离线测试通过，核心引擎可执行行覆盖率 99.7%。真实目录审计见
[data/feegate_schema_audit.json](../data/feegate_schema_audit.json)：扫描完整，12 份资产仍有历史合规缺口，未自动修改商品。
全仓按模块隔离回归，58 个模块通过，8 个旧模块失败，2 个音频模块超时；完整审计记录见
[data/feegate_schema_audit.json](../data/feegate_schema_audit.json)。
