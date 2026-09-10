# 岗位要求 Grilling —— 用追问把你的筛选标准变成 `filter_rules.yaml`

> 交给 AI 助手（WorkBuddy 等）执行。目标：通过**追问**把"哪些岗位投、哪些不投"
> 问清楚，最后生成 `filter_rules.yaml`。**生成前不要改任何代码。**

## 你要做的

1. 先读 `filter_rules.example.yaml`，知道有哪些可配置的旋钮。
2. 像资深猎头一样**分轮追问**，每轮把「彼此独立、不依赖后续答案」的问题一次问完，
   每个问题都给出**你的推荐答案**，等对方拍板后再追问下一轮（被当前问题决定的问题，
   下一轮再问，不要混在一轮里）。
3. 追问要覆盖下面这些面，直到没有含糊：
   - **一票否决**：哪些行业/公司/岗位关键词，看到就直接不投？
   - **行业底线**：只投哪些行业？还是不限？
   - **职能范围**：只投哪些岗位类型（如 商业分析 / 数据分析 / 运营 / 销售 / BD）？
     有没有"只要不是 XX"这类排除？
   - **专业要求**：JD 写什么专业你会被卡？哪些专业算"可接受"？
     （典型：非理工科背景遇到"理工科优先"就该拒）
   - **经验门槛**：要求几年以上经验就不投？"1-3 年"这类写法怎么算？
   - **薪资**：月薪下限多少？**薪资没写**时算通过还是拒绝？
   - **其他**：兼职/实习要不要？电话销售类要不要？是否只要海外/跨境岗？
4. 每轮结束后把已确定的点复述一遍，确认无误解。
5. 全部谈拢后，按下面的口径把结果写进 `filter_rules.yaml`（照 `filter_rules.example.yaml` 的字段）：

| 追问里得到的东西 | 落到哪个字段 |
| --- | --- |
| 看到就不投的**岗位/标题**词 | `reject.title_keywords` |
| 看到就不投的**公司/雇主**词 | `reject.employer_keywords` |
| 看到就不投的**行业** | `reject.industries` |
| JD 里会卡你的**专业** | `reject.required_majors` |
| 可接受的专业 | `require.allowed_majors` |
| 不投的**技术岗**关键词 | `reject.dev_keywords` |
| 电话销售 / 兼职实习 | `reject.phone_sales` / `reject.part_time` |
| 要求 N 年以上经验就不投 | `reject.min_years_experience = N` |
| "JD 写死要经验"就不投 | `reject.experience_required = true` |
| 只投的**行业** | `require.industries` |
| 只投的**职能** | `require.functions` |
| 只要海外/跨境 | `require.overseas = true` |
| 月薪下限 / 薪资不明策略 | `salary.floor` / `salary.unknown` |

补充口径：
- 列表条目是**正则**，可以直接写 `电商(?!支付|收款)` 这类负向断言来保留边界情况。
- 留空 = 不启用该限制——**不要替用户加他没说的约束**。
- `reject.scam_title` 建议保持 `true`（标题挂薪资的岗位多为骗子岗）。

6. 写完 `filter_rules.yaml` 后，把最终规则用一张表复述给对方确认；确认通过再运行
   `boss_batch.py`。

## 完成后提示对方

```bash
.venv/Scripts/python.exe boss_batch.py --query "商业分析" --limit 10
```
