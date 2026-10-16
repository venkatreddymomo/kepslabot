/**
 * Shared bits for the per-embed AI model pin.
 *
 * A widget deployment may pin a chat model. When pinned, every real guest turn
 * on that embed uses that model (and that model's prompt variant) and the /chat
 * test-harness dropdown cannot move it — so QA can switch models freely without
 * touching a live customer.
 *
 * The model is stored on the deployment row and resolved server-side from the
 * widget_key. It is deliberately NOT written into the embed snippet: the snippet
 * sits on the customer's website where anyone could edit it, and the model
 * decides what gets billed. Keeping it server-side also means the model can be
 * changed later without any customer re-pasting their code.
 */

/**
 * Radix Select forbids an empty-string item value, so "unpinned" needs a
 * sentinel in the UI. Map it back to "" before sending to the API.
 */
export const UNPINNED_MODEL = "__unpinned__";

export interface ModelOption {
  id: string;
  label: string;
}
