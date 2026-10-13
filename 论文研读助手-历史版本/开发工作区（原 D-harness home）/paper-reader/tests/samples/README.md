# 阶段 8 样例分析文件

这两个 JSON 由 `python tests/capture_phase8_samples.py` 生成，格式与落盘的
`data/analyses/<paper_id>.json` 完全一致（同一个 `reading_card` 校验器与 `storage` 写入器）。

**说明**：引文、页码与 `verification` 区块都是真实的——引文取自论文自己的文本层，
并逐条通过引文核对；答案文字来自测试夹具，用来演示字段格式与页码可追溯性，
不代表任何具体模型的措辞。要得到真实模型的措辞，请把本机 Ollama / LM Studio
接入后在界面上点击“查看 AI 参考”。

字段级页码索引见 `index.json`。
