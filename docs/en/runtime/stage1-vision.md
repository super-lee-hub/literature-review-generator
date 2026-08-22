# Stage 1 Vision-First Pipeline

Stage 1 keeps MinerU or other normalized full text as a primary evidence
source and adds page-level visual evidence. The pipeline does not guess which
figure is important before the model has seen the page.

## Coverage

Every nonblank PDF page is rendered as a traceable `page_snapshot`. Blank pages
may be skipped only with `skipped_blank`; render failures remain explicit
`render_failed`. The durable `stage1_visual_coverage/v1` report records total
pages, nonblank pages, rendered/scanned/skipped/failed counts, page status,
crop references, batches, coverage status, and omissions.

## Request paths

- Short papers (at most `single_call_max_pages`) use one synthesis request with
  MinerU full text, page metadata, page-specific labels/OCR, page images, and
  bounded figure/table/formula crops.
- Longer papers first use deterministic visual scan batches of at most
  `visual_scan_batch_size` page objects. Each image is immediately preceded by
  a text label containing `visual_id`, page, bbox, artifact type, caption, and
  nearby OCR/text. The scan output is a durable
  `stage1_visual_observations/v1` artifact.
- Final synthesis receives the full normalized text, all scan observations,
  coverage report, and at most `final_image_refs_max` high-value crops. Crop
  selection is a second-stage evidence decision; it does not decide whether a
  page was visible to the visual model.

## Provider and fallback

DeepSeek Vision uses the official OpenAI-compatible Chat Completions format:
`text` blocks and `image_url` blocks containing base64 data URLs. Responses
uses `input_text` and `input_image`. The experimental model is explicitly
capability-gated; ordinary `deepseek-v4-flash` is text-only.

If the vision call fails, the reader falls back to ordinary Flash with MinerU
full text and any successful visual observations rendered as text. A fallback
is recorded as fallback evidence and is never labeled multimodal success.

Validation stays on ordinary `deepseek-v4-flash` and receives source chunks,
OCR, captions, and observation text only. Original PDFs are not sent as file
attachments by default.

## Official limits

The implementation follows the [DeepSeek Vision guide](https://api-docs.deepseek.com/zh-cn/guides/vision): 48 MiB inline request body, 32 MiB per base64/URL image, 384 image-token upper bound, and 4096 px single-edge limit when a request contains 15 or more images.
