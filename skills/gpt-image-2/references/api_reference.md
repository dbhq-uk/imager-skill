# GPT Image API Reference

Checked against `openai/openai-openapi` (`CreateImageRequest`, `CreateImageEditRequest`,
`ImagesResponse`) and the image generation guide on 2026-09-12.

## Endpoints

**Generation:** `POST https://api.openai.com/v1/images/generations`
**Editing:** `POST https://api.openai.com/v1/images/edits`

## Authentication

```
Authorization: Bearer <OPENAI_API_KEY>
Content-Type: application/json      (generations)
Content-Type: multipart/form-data   (edits)
```

## Models

| Model | Notes |
|-------|-------|
| `gpt-image-2.5-flare` | Fast everyday generation. Adds `xhigh` and `max` quality. |
| `gpt-image-2.5-sunburst` | Precision editing, fine detail, dense text. Slower. Adds `xhigh` and `max`. |
| `gpt-image-2` | Previous generation. Quality up to `high`. Eligible for the Batch API discount. |
| `gpt-image-1.5`, `gpt-image-1-mini`, `chatgpt-image-latest` | **Removed from the API on 1 December 2026.** Replacement is `gpt-image-2`. |
| `gpt-image-1` | Removed 23 October 2026. |
| `dall-e-2`, `dall-e-3` | Deprecated 12 May 2026. |

Dated snapshots exist: `gpt-image-2-2026-04-21`, `gpt-image-2.5-sunburst-2026-09-08`,
`gpt-image-2.5-flare-2026-09-08`.

## Generation Request Body

```json
{
  "model": "gpt-image-2.5-flare",
  "prompt": "a cat wearing a space suit",
  "n": 1,
  "size": "1024x1024",
  "quality": "low",
  "background": "auto",
  "output_format": "png",
  "output_compression": 100,
  "moderation": "auto"
}
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model` | string | `dall-e-2` | Always send it explicitly. |
| `prompt` | string | required | Up to 32,000 characters for GPT image models. |
| `n` | integer | 1 | 1-10. |
| `size` | string | `auto` | `WIDTHxHEIGHT`, or `auto`. See constraints below. |
| `quality` | string | `auto` | `low`, `medium`, `high`; plus `xhigh`, `max` on the 2.5 models. |
| `background` | string | `auto` | `transparent`, `opaque`, `auto`. Transparent needs `output_format` png or webp. On `gpt-image-2` it is in preview. |
| `output_format` | string | `png` | `png`, `jpeg`, `webp`. |
| `output_compression` | integer | 100 | 0-100. `jpeg` and `webp` only. |
| `moderation` | string | `auto` | `auto` or `low` (less restrictive). |
| `stream` | boolean | false | Streaming mode. |
| `partial_images` | integer | - | Progressive previews. Each partial costs an extra 100 image output tokens. |
| `user` | string | - | End-user identifier for abuse monitoring. |
| `response_format` | string | `url` | **Not supported on GPT image models** - they always return base64. |

### Size constraints (GPT Image 2 and 2.5)

Arbitrary resolutions are accepted as `WIDTHxHEIGHT` when all of these hold:

- both edges divisible by **16**
- aspect ratio between **1:3** and **3:1**
- longest edge **3840px** or less
- total pixels between **655,360** and **8,294,400**

Resolutions above `2560x1440` are experimental. Standard sizes: `1024x1024` (square),
`1536x1024` (landscape), `1024x1536` (portrait).

### Parameters that do NOT exist

There is no `seed` and no `thinking` parameter on either endpoint, for any GPT image model.
Neither appears in the OpenAPI schema or in the guide. Generation is not reproducible; carry
consistency in the prompt and in reference images instead.

## Edit Request (multipart/form-data)

| Field | Notes |
|-------|-------|
| `image` / `image[]` | Up to 16 png/webp/jpg files, each under 50MB. |
| `prompt` | Up to 32,000 characters. |
| `mask` | PNG, under 4MB, same dimensions as the image. **Fully transparent areas mark what to replace.** Applied to the first image when several are sent. Prompt-guided: the model treats the shape as guidance, not a hard boundary. |
| `background`, `output_format`, `output_compression`, `moderation`, `size`, `quality`, `n` | As above. |
| `input_fidelity` | Not settable for `gpt-image-2` - it processes every image input at high fidelity automatically, which also makes input tokens higher on edits carrying references. |

## Response

```json
{
  "created": 1713833628,
  "data": [{ "b64_json": "..." }],
  "background": "transparent",
  "output_format": "png",
  "size": "1024x1024",
  "quality": "high",
  "usage": {
    "total_tokens": 4310,
    "input_tokens": 150,
    "output_tokens": 4160,
    "input_tokens_details": { "text_tokens": 50, "image_tokens": 100 }
  }
}
```

The response echoes the settings actually applied, which matters when `auto` was sent.

## Pricing

Per 1,000,000 tokens, standard tier. `gpt-image-2`, `gpt-image-2.5-flare` and
`gpt-image-2.5-sunburst` currently share these rates:

| Token type | Standard | Batch |
|-----------|----------|-------|
| Text input | $5.00 | $2.50 |
| Cached text input | $1.25 | $0.625 |
| Image input | $8.00 | $4.00 |
| Cached image input | $2.00 | $1.00 |
| Image output | $30.00 | $15.00 |

**The Batch API's 50% discount is published for `gpt-image-2` and not for the 2.5 models.**

Equal token rates do not mean equal cost per image: the models spend different numbers of output
tokens for the same quality setting. Compute the real figure from `usage`:

```
cost = text_tokens x $5/1M + image_input_tokens x $8/1M + output_tokens x $30/1M
```

### Published per-image costs, gpt-image-2

| Quality | 1024x1024 | 1024x1536 | 1536x1024 |
|---------|-----------|-----------|-----------|
| Low | $0.006 | $0.005 | $0.005 |
| Medium | $0.053 | $0.041 | $0.041 |
| High | $0.211 | $0.165 | $0.165 |

A larger non-square resolution can produce fewer output tokens than a square one at the same
quality. OpenAI publishes no equivalent table for the 2.5 models.

## Errors

| Code | Meaning | Retry? |
|------|---------|--------|
| 400 | Bad request / invalid params | No |
| 401 | Invalid API key | No |
| 403 | Content policy | No |
| 429 | Rate limit exceeded | Yes (backoff) |
| 500 / 502 | Server error | Yes (backoff) |

Some failures are user-correctable and carry `error.type = "image_generation_user_error"`. Use
`error.code` as the stable discriminator, and never auto-retry these. When
`error.code = "moderation_blocked"`, the error may include:

```json
{
  "error": {
    "type": "image_generation_user_error",
    "code": "moderation_blocked",
    "moderation_details": {
      "moderation_stage": "input",
      "categories": ["harassment"]
    }
  }
}
```

`moderation_stage` is `input`, `output` or `unknown`. `categories` holds coarse public labels
such as `harassment`, `self-harm`, `sexual`, `violence`.

## Other notes

- Complex prompts can take up to two minutes. The CLI uses a 900s read timeout.
- Outputs carry C2PA metadata and invisible watermarking.
- The Responses API exposes image generation as a built-in tool, with multi-turn editing, file
  ID inputs and `input_image_mask`. This CLI uses the Image API only.
- Organisation verification may be required before GPT Image models are available.

## OpenRouter

Same request format, different base URL:

- **Generation:** `POST https://openrouter.ai/api/v1/images/generations`
- **Editing:** `POST https://openrouter.ai/api/v1/images/edits`
- Extra headers: `HTTP-Referer`, `X-Title`

Model availability and parameter support differ from OpenAI's own endpoint - check before a
billable run.
