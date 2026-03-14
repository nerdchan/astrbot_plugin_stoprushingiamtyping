# astrbot_plugin_stoprushingiamtyping

Discord typing 指示插件：在 AstrBot 進入 LLM 推理階段時，持續顯示「輸入中」直到回覆送出。

此版本參考 [Satori Discord adapter](https://github.com/satorijs/satori.git) 的分層概念：

- 事件層：接收 AstrBot 事件 hook。
- 解析層：從 event 安全提取 Discord channel。
- Internal 層：統一觸發 Discord typing API。
- 控制層：以 keepalive task 管理每個 session 的 typing 生命週期。

## 功能

- `@filter.on_llm_request()` 開始 typing keepalive。
- `@filter.after_message_sent()` 立即停止該會話 typing。
- 支援多來源 channel id 解析（event/message/raw_message/unified origin）。
- 支援多種 Discord 客戶端觸發方式（`channel.trigger_typing()` 與 HTTP fallback）。
- 具備 session generation guard，避免舊 task 競態覆寫。
- 具備最長 typing 視窗，避免異常卡住。

## 安裝

1. 將本插件目錄放在 AstrBot 的 `data/plugins` 下。
2. 安裝依賴：

```bash
pip install -r requirements.txt
```

3. 在 WebUI 啟用插件並調整配置。

## 配置項

- `enable`：是否啟用插件。
- `typing_keepalive_seconds`：續命間隔，建議 6~9 秒。
- `max_typing_window_seconds`：單次 typing 最長維持秒數。
- `debug_log`：是否輸出除錯日誌。

## 注意事項

- 本插件只對 Discord 事件生效。
- 需要 Bot 具備頻道可見與發言權限，才能穩定顯示 typing。
- 若事件中無法解析 channel，插件會自動降級跳過 typing，不阻斷正常回覆。
