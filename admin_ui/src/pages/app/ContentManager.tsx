import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  Loader2,
  RefreshCcw,
  Trash2,
  Upload,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  adminGet,
  adminSend,
  getActivePropertyCode,
  normalizePropertyCode,
  setActivePropertyCode,
} from "@/lib/adminApi";
import {
  scraperApi,
  type JobSummary,
  type ReviewEntity,
  type ReviewItem,
  type ReviewPayload,
} from "@/lib/scraperApi";

function statusLabel(status: string) {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function itemDisplayName(item: ReviewItem) {
  return item.name || item.suggested_name || item.url || item.id;
}

function entityDisplayName(entity: ReviewEntity) {
  return entity.name || entity.suggested_name || entity.id;
}

type PropertyOption = {
  code: string;
  name: string;
  city: string;
};

type SubPropertyOption = {
  id: number;
  slug: string;
  name: string;
  is_default: boolean;
};

type HotelImageRow = {
  id: number;
  hotel_id: number;
  property_id: number | null;
  title: string;
  description: string;
  image_url: string;
  // True when the row is an S3 upload: image_url is a transient presigned link
  // and must not be edited / sent back on save (the backend ignores it anyway).
  is_uploaded?: boolean;
  category: string;
  tags: string[];
  source_label: string;
  is_active: boolean;
  priority: number;
};

type EditableHotelImage = HotelImageRow & {
  draftTitle: string;
  draftImageUrl: string;
  draftSourceLabel: string;
  draftCategory: string;
  dirty: boolean;
};

// What a photo shows, as the chat reads it (api/routes/admin.py CONTENT_IMAGE_CATEGORIES).
// "Exterior" and "Room" make up the gallery a guest sees on picking the hotel; the rest are
// shown in chat when the reply names them (the Title is what gets matched).
const IMAGE_CATEGORY_OPTIONS: { value: string; label: string }[] = [
  { value: "facade", label: "Exterior / building — shown when a guest picks the hotel" },
  { value: "room", label: "Room (main shot) — shown when a guest picks the hotel" },
  { value: "room_detail", label: "Room detail (bathroom, other angles) — shown in chat" },
  { value: "dining", label: "Restaurant / bar / café" },
  { value: "pool", label: "Pool" },
  { value: "spa", label: "Spa" },
  { value: "banquet", label: "Banquet / meeting hall" },
  { value: "lobby", label: "Lobby / reception" },
  { value: "fitness", label: "Gym / fitness" },
  { value: "kids", label: "Kids area" },
  { value: "other", label: "Other — not shown unless its title is named" },
];

function imageCategoryLabel(value: string): string {
  return IMAGE_CATEGORY_OPTIONS.find((o) => o.value === value)?.label.split(" — ")[0] || value;
}

// The bot finds a photo in chat by the words of its Title. Camera and phone file names
// ("JCR_1690", "IMG_9966", "PXL_20261005_101112") have none, so such a photo never shows.
const FILE_NAME_WORDS = new Set([
  "img", "jcr", "dsc", "dscn", "dcim", "pxl", "image", "photo", "pic", "picture",
  "jpeg", "jpg", "png", "tif", "tiff", "webp", "copy", "edited", "final", "scan",
]);

function looksLikeFileName(text: string): boolean {
  const words = String(text || "").toLowerCase().match(/[a-z]{3,}/g) || [];
  return words.filter((w) => !FILE_NAME_WORDS.has(w)).length === 0;
}

const FILE_NAME_TITLE_HINT =
  "This looks like a file name. The bot finds photos by the words in the title — write what the photo shows, e.g. “Deluxe King Room – bathroom” or “Rooftop pool”.";

type HotelImagesResponse = {
  hotel?: {
    id?: number;
    code?: string;
    name?: string;
    city?: string;
  };
  images?: HotelImageRow[];
  total?: number;
};

type HotelSubPropertiesResponse = {
  hotel_id?: number | null;
  properties?: SubPropertyOption[];
};

type HotelImageUpdateResponse = {
  image?: HotelImageRow;
};

function toEditableImage(row: HotelImageRow): EditableHotelImage {
  return {
    ...row,
    property_id: row.property_id === undefined ? null : row.property_id,
    draftTitle: String(row.title || ""),
    draftImageUrl: String(row.image_url || ""),
    draftSourceLabel: String(row.source_label || ""),
    draftCategory: String(row.category || ""),
    dirty: false,
  };
}

function isHttpImageUrl(value: string): boolean {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized.startsWith("http://") || normalized.startsWith("https://");
}

type BulkPreviewRow = {
  row: number;
  label: string;
  image_url: string;
  category?: string | null;
};

type BulkFailedRow = {
  row: number;
  label: string;
  image_url: string;
  reason: string;
};

type BulkPreviewResponse = {
  filename: string;
  sheet_names: string[];
  selected_sheet: string;
  header_skipped: boolean;
  totals: { valid: number; failed: number };
  preview: BulkPreviewRow[];
  failed: BulkFailedRow[];
};

type BulkCommitResponse = {
  filename: string;
  sheet_names: string[];
  header_skipped: boolean;
  totals: { inserted: number; updated: number; failed: number };
  failed: BulkFailedRow[];
};

function csvEscape(value: string): string {
  const s = String(value ?? "");
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function buildFailedRowsCsv(failed: BulkFailedRow[]): string {
  const lines = ["row,label,image_url,reason"];
  for (const f of failed) {
    lines.push(
      [String(f.row), csvEscape(f.label), csvEscape(f.image_url), csvEscape(f.reason)].join(","),
    );
  }
  return lines.join("\r\n");
}

function downloadFailedRowsCsv(failed: BulkFailedRow[], baseName: string) {
  if (failed.length === 0) return;
  const csv = buildFailedRowsCsv(failed);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const stem = (baseName || "bulk_upload").replace(/\.[^.]+$/, "") || "bulk_upload";
  a.download = `${stem}_skipped_rows.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function EntityBlock({
  entity,
  onChange,
}: {
  entity: ReviewEntity;
  onChange: (updated: ReviewEntity) => void;
}) {
  const [expanded, setExpanded] = useState(true);

  const pages = entity.items.filter((i) => i.type === "page");
  const assets = entity.items.filter((i) => i.type !== "page");

  function toggleEntity(checked: boolean) {
    onChange({
      ...entity,
      enabled: checked,
      items: entity.items.map((i) => ({ ...i, enabled: checked })),
    });
  }

  function toggleItem(id: string, checked: boolean) {
    onChange({
      ...entity,
      items: entity.items.map((i) =>
        i.id === id ? { ...i, enabled: checked } : i,
      ),
    });
  }

  const enabledCount = pages.filter((p) => p.enabled !== false).length;

  return (
    <Card className="mb-3">
      <CardHeader className="px-4 py-3">
        <div className="flex items-center gap-3">
          <Checkbox
            id={`entity-${entity.id}`}
            checked={entity.enabled !== false}
            onCheckedChange={(v) => toggleEntity(!!v)}
          />
          <Label
            htmlFor={`entity-${entity.id}`}
            className="flex-1 cursor-pointer text-base font-semibold"
          >
            {entityDisplayName(entity)}
          </Label>
          <span className="text-xs text-muted-foreground">
            {enabledCount}/{pages.length} pages
          </span>
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-muted-foreground"
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </div>
      </CardHeader>

      {expanded && (
        <CardContent className="px-4 pb-4 pt-0">
          {pages.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pages found.</p>
          ) : (
            <ul className="space-y-1.5">
              {pages.map((page) => (
                <li key={page.id} className="flex items-start gap-2">
                  <Checkbox
                    id={`page-${page.id}`}
                    checked={page.enabled !== false}
                    onCheckedChange={(v) => toggleItem(page.id, !!v)}
                    className="mt-0.5"
                  />
                  <Label
                    htmlFor={`page-${page.id}`}
                    className="cursor-pointer text-sm"
                  >
                    <span className="font-medium">{itemDisplayName(page)}</span>
                    {page.url && (
                      <span className="block max-w-sm truncate text-xs text-muted-foreground">
                        {page.url}
                      </span>
                    )}
                  </Label>
                </li>
              ))}
            </ul>
          )}
          {assets.length > 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              +{assets.length} media asset{assets.length !== 1 ? "s" : ""}
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}

export default function ContentManager() {
  const [propertyCode, setPropertyCode] = useState(getActivePropertyCode());
  const [propertyOptions, setPropertyOptions] = useState<PropertyOption[]>([]);
  const [loadingProperties, setLoadingProperties] = useState(true);
  // Sub-properties live under a single hotel (new_bot_properties). When the
  // active hotel has more than one, we render a second dropdown so admins can
  // scope image uploads (and the list view) to a specific sub-property.
  //
  // subPropertyValue is a single union string:
  //   ""        → no filter / shared scope for uploads (matches today's behavior)
  //   "shared"  → list filter: only rows where property_id IS NULL
  //   "<id>"    → list filter + upload scope for that specific sub-property
  const [subProperties, setSubProperties] = useState<SubPropertyOption[]>([]);
  const [subPropertyValue, setSubPropertyValue] = useState<string>("");
  const [loadingSubProperties, setLoadingSubProperties] = useState(false);
  const [hotelImages, setHotelImages] = useState<EditableHotelImage[]>([]);
  const [loadingHotelImages, setLoadingHotelImages] = useState(false);
  const [hotelImagesStatus, setHotelImagesStatus] = useState<"idle" | "ok" | "err">("idle");
  const [hotelImagesMsg, setHotelImagesMsg] = useState("");
  const [savingImageId, setSavingImageId] = useState<number | null>(null);
  const [deletingImageId, setDeletingImageId] = useState<number | null>(null);
  const [imageRowMessages, setImageRowMessages] = useState<Record<number, { type: "ok" | "err"; text: string }>>({});
  const [newImageTitle, setNewImageTitle] = useState("");
  const [newImageUrl, setNewImageUrl] = useState("");
  const [newImageSourceLabel, setNewImageSourceLabel] = useState("");
  const [newImageCategory, setNewImageCategory] = useState("");
  const [showUncategorisedOnly, setShowUncategorisedOnly] = useState(false);
  const [creatingImage, setCreatingImage] = useState(false);
  const [previewImage, setPreviewImage] = useState<{ url: string; title: string } | null>(null);

  // Add-image input mode: paste a URL (original behavior) or upload a file to S3.
  const [newImageMode, setNewImageMode] = useState<"url" | "upload">("url");
  const newImageFileRef = useRef<HTMLInputElement | null>(null);
  const [newImageFile, setNewImageFile] = useState<File | null>(null);
  const [newImageFilePreview, setNewImageFilePreview] = useState<string>("");
  const [uploadingImage, setUploadingImage] = useState(false);

  const ALLOWED_UPLOAD_TYPES = ["image/png", "image/jpeg", "image/webp"];
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

  const bulkFileInputRef = useRef<HTMLInputElement | null>(null);
  const [bulkFile, setBulkFile] = useState<File | null>(null);
  const [bulkSheet, setBulkSheet] = useState<string>("");
  const [bulkPreview, setBulkPreview] = useState<BulkPreviewResponse | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkCommitResponse | null>(null);
  const [bulkLoading, setBulkLoading] = useState(false);
  const [bulkCommitting, setBulkCommitting] = useState(false);
  const [bulkError, setBulkError] = useState("");

  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const [reviewPayload, setReviewPayload] = useState<ReviewPayload | null>(null);
  const [loadingReview, setLoadingReview] = useState(false);
  const [publishStatus, setPublishStatus] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [publishMsg, setPublishMsg] = useState("");

  const fetchProperties = useCallback(async () => {
    setLoadingProperties(true);
    try {
      const payload = await adminGet<{ properties?: Array<{ id?: string; code?: string; name?: string; city?: string }> }>(
        "/properties",
        propertyCode,
      );
      const rows = Array.isArray(payload?.properties)
        ? payload.properties
            .map((row) => {
              const code = normalizePropertyCode(String(row?.id || row?.code || ""));
              return {
                code,
                name: String(row?.name || code).trim() || code,
                city: String(row?.city || "").trim(),
              };
            })
            .filter((row) => row.code)
        : [];

      const mergedRows =
        rows.length > 0
          ? rows
          : [{ code: normalizePropertyCode(propertyCode) || "default", name: normalizePropertyCode(propertyCode) || "default", city: "" }];
      setPropertyOptions(mergedRows);

      const activeCode = normalizePropertyCode(propertyCode) || "default";
      const hasActive = mergedRows.some((row) => row.code === activeCode);
      if (!hasActive && mergedRows[0]?.code) {
        setPropertyCode(mergedRows[0].code);
        setActivePropertyCode(mergedRows[0].code);
      }
    } catch {
      const fallbackCode = normalizePropertyCode(propertyCode) || "default";
      setPropertyOptions([{ code: fallbackCode, name: fallbackCode, city: "" }]);
    } finally {
      setLoadingProperties(false);
    }
  }, [propertyCode]);

  // Translate the union dropdown value into a query string the backend
  // understands. Keep this in one place so list / refetch share the same logic.
  const buildImagesQueryString = useCallback((subPropValue: string): string => {
    const v = String(subPropValue || "").trim();
    if (!v) return "";
    if (v === "shared") return "?property_id=shared";
    const parsed = Number(v);
    if (Number.isFinite(parsed) && parsed > 0) return `?property_id=${parsed}`;
    return "";
  }, []);

  // Same translation for write paths (POST/PUT body, bulk FormData). Returns
  //   null  → shared / no scope (column stays NULL)
  //   <int> → scope to this specific sub-property
  const resolvePropertyIdForWrite = useCallback(
    (subPropValue: string): number | null => {
      const v = String(subPropValue || "").trim();
      if (!v || v === "shared") return null;
      const parsed = Number(v);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    },
    [],
  );

  const fetchHotelImages = useCallback(
    async (targetPropertyCode?: string, subPropValueOverride?: string) => {
      const scopedCode = normalizePropertyCode(targetPropertyCode || propertyCode) || "default";
      const subPropValue = subPropValueOverride !== undefined ? subPropValueOverride : subPropertyValue;
      setLoadingHotelImages(true);
      setHotelImagesStatus("idle");
      setHotelImagesMsg("");
      setImageRowMessages({});
      try {
        const qs = buildImagesQueryString(subPropValue);
        const payload = await adminGet<HotelImagesResponse>(`/content/hotel-images${qs}`, scopedCode);
        const rows = Array.isArray(payload?.images) ? payload.images : [];
        setHotelImages(rows.map(toEditableImage));
      } catch (e) {
        setHotelImages([]);
        setHotelImagesStatus("err");
        setHotelImagesMsg(e instanceof Error ? e.message : "Failed to load hotel images");
      } finally {
        setLoadingHotelImages(false);
      }
    },
    [propertyCode, subPropertyValue, buildImagesQueryString],
  );

  const fetchSubProperties = useCallback(async (targetPropertyCode?: string) => {
    const scopedCode = normalizePropertyCode(targetPropertyCode || propertyCode) || "default";
    setLoadingSubProperties(true);
    try {
      const payload = await adminGet<HotelSubPropertiesResponse>(
        "/hotel-properties",
        scopedCode,
      );
      const rows = Array.isArray(payload?.properties) ? payload.properties : [];
      setSubProperties(rows);
      // NOTE: we deliberately do NOT reset subPropertyValue here. The reset
      // belongs in the propertyCode useEffect (fires only on actual hotel
      // changes); doing it here would clobber the user's selection any time
      // this fetcher is invoked, including the "refresh" button path.
    } catch {
      setSubProperties([]);
    } finally {
      setLoadingSubProperties(false);
    }
  }, [propertyCode]);

  const fetchJobs = useCallback(async () => {
    try {
      const list = await scraperApi.listJobs();
      setJobs(list.filter((j) => j.can_open_review || j.status === "completed"));
    } catch {
      // ignore
    } finally {
      setLoadingJobs(false);
    }
  }, []);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    fetchProperties();
  }, [fetchProperties]);

  useEffect(() => {
    const scopedCode = normalizePropertyCode(propertyCode) || "default";
    setActivePropertyCode(scopedCode);
    // Reset sub-property filter when the hotel changes — a previously-picked
    // sub-property id is meaningless under a different hotel.
    setSubPropertyValue("");
    fetchSubProperties(scopedCode);
    // Fetch the "all" view explicitly. If subPropertyValue was already "" we
    // need this; if it was non-empty, the reset above will retrigger the
    // subPropertyValue effect (cheap duplicate, but never inconsistent).
    fetchHotelImages(scopedCode, "");
    // Intentionally depend ONLY on propertyCode. Including fetchHotelImages /
    // fetchSubProperties caused a state loop: changing subPropertyValue
    // rebuilt the fetchHotelImages callback ref, this effect re-fired, which
    // reset subPropertyValue back to "" and snapped the dropdown to "All".
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propertyCode]);

  // When the sub-property selection changes, refresh the image list with the
  // new filter. Hotel didn't change, so we don't refetch sub-properties.
  useEffect(() => {
    const scopedCode = normalizePropertyCode(propertyCode) || "default";
    fetchHotelImages(scopedCode, subPropertyValue);
    // Same reason as above — keep this effect listening to subPropertyValue
    // alone so we don't double-fire when callbacks rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subPropertyValue]);

  // Reset any in-flight bulk-upload state when the active property OR
  // sub-property changes, so a previewed file can never get committed
  // against the wrong scope.
  useEffect(() => {
    setBulkFile(null);
    setBulkPreview(null);
    setBulkResult(null);
    setBulkSheet("");
    setBulkError("");
    if (bulkFileInputRef.current) {
      bulkFileInputRef.current.value = "";
    }
  }, [propertyCode, subPropertyValue]);

  // Revoke the in-memory object URL when the preview changes or the page
  // unmounts so selecting many files in a row doesn't leak blob URLs.
  useEffect(() => {
    return () => {
      if (newImageFilePreview) URL.revokeObjectURL(newImageFilePreview);
    };
  }, [newImageFilePreview]);

  async function loadReview(jobId: string) {
    setSelectedJobId(jobId);
    setReviewPayload(null);
    setPublishStatus("idle");
    setPublishMsg("");
    setLoadingReview(true);
    try {
      const res = await scraperApi.getReview(jobId);
      setReviewPayload(res.review_data);
    } catch (e) {
      setPublishMsg(e instanceof Error ? e.message : "Failed to load review data");
      setPublishStatus("err");
    } finally {
      setLoadingReview(false);
    }
  }

  function updateEntity(updated: ReviewEntity) {
    if (!reviewPayload) return;
    setReviewPayload({
      ...reviewPayload,
      entities: reviewPayload.entities.map((e) =>
        e.id === updated.id ? updated : e,
      ),
    });
  }

  async function handlePublish() {
    if (!selectedJobId || !reviewPayload) return;
    setPublishStatus("busy");
    setPublishMsg("");
    try {
      await scraperApi.publish(selectedJobId, reviewPayload);
      setPublishStatus("ok");
      setPublishMsg("Publishing started. Check the Web Crawl page for progress.");
      await fetchJobs();
    } catch (e) {
      setPublishStatus("err");
      setPublishMsg(e instanceof Error ? e.message : "Publish failed");
    }
  }

  function updateImageDraft(
    imageId: number,
    field: "draftTitle" | "draftImageUrl" | "draftSourceLabel" | "draftCategory",
    value: string,
  ) {
    setHotelImages((prev) =>
      prev.map((row) => {
        if (row.id !== imageId) return row;
        const nextDraftTitle = field === "draftTitle" ? value : row.draftTitle;
        const nextDraftImageUrl = field === "draftImageUrl" ? value : row.draftImageUrl;
        const nextDraftSourceLabel = field === "draftSourceLabel" ? value : row.draftSourceLabel;
        const nextDraftCategory = field === "draftCategory" ? value : row.draftCategory;
        const dirty =
          nextDraftTitle.trim() !== row.title ||
          nextDraftImageUrl.trim() !== row.image_url ||
          nextDraftSourceLabel.trim() !== (row.source_label || "") ||
          nextDraftCategory !== (row.category || "");
        return {
          ...row,
          draftTitle: nextDraftTitle,
          draftImageUrl: nextDraftImageUrl,
          draftSourceLabel: nextDraftSourceLabel,
          draftCategory: nextDraftCategory,
          dirty,
        };
      }),
    );
    setImageRowMessages((prev) => {
      if (!(imageId in prev)) return prev;
      const next = { ...prev };
      delete next[imageId];
      return next;
    });
  }

  async function saveHotelImage(row: EditableHotelImage) {
    const nextTitle = row.draftTitle.trim();
    const nextImageUrl = row.draftImageUrl.trim();
    const nextSourceLabel = row.draftSourceLabel.trim();
    if (!nextTitle) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: "Title is required" },
      }));
      return;
    }
    // Uploaded rows have a presigned (non-editable) URL; only paste-URL rows
    // validate + send image_url. The backend ignores image_url for uploads.
    if (!row.is_uploaded && !isHttpImageUrl(nextImageUrl)) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: "Image URL must start with http:// or https://" },
      }));
      return;
    }

    setSavingImageId(row.id);
    try {
      const body: Record<string, unknown> = {
        title: nextTitle,
        source_label: nextSourceLabel || null,
      };
      if (!row.is_uploaded) {
        body.image_url = nextImageUrl;
      }
      // Only sent when changed, so saving a title never touches the category.
      if (row.draftCategory !== (row.category || "")) {
        body.category = row.draftCategory;
      }
      const payload = await adminSend<HotelImageUpdateResponse>(
        "PUT",
        `/content/hotel-images/${row.id}`,
        body,
        propertyCode,
      );
      const updated = payload?.image;
      if (updated) {
        setHotelImages((prev) =>
          prev.map((item) => (item.id === row.id ? toEditableImage(updated) : item)),
        );
      } else {
        setHotelImages((prev) =>
          prev.map((item) =>
            item.id === row.id
              ? {
                  ...item,
                  title: nextTitle,
                  image_url: nextImageUrl,
                  source_label: nextSourceLabel,
                  category: row.draftCategory,
                  draftTitle: nextTitle,
                  draftImageUrl: nextImageUrl,
                  draftSourceLabel: nextSourceLabel,
                  dirty: false,
                }
              : item,
          ),
        );
      }
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "ok", text: "Saved" },
      }));
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image changes saved");
    } catch (e) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: e instanceof Error ? e.message : "Save failed" },
      }));
    } finally {
      setSavingImageId(null);
    }
  }

  async function createHotelImage() {
    const title = newImageTitle.trim();
    const imageUrl = newImageUrl.trim();
    const sourceLabel = newImageSourceLabel.trim();
    if (!title) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Title is required");
      return;
    }
    if (looksLikeFileName(title)) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(FILE_NAME_TITLE_HINT);
      return;
    }
    if (!isHttpImageUrl(imageUrl)) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Image URL must start with http:// or https://");
      return;
    }
    if (!newImageCategory) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Choose what the photo shows (Category) so the bot knows when to show it");
      return;
    }

    setCreatingImage(true);
    try {
      const payload = await adminSend<HotelImageUpdateResponse>(
        "POST",
        "/content/hotel-images",
        {
          title,
          image_url: imageUrl,
          source_label: sourceLabel || null,
          property_id: resolvePropertyIdForWrite(subPropertyValue),
          category: newImageCategory,
        },
        propertyCode,
      );
      const created = payload?.image;
      if (created) {
        // Only show the new row inline if it matches the current sub-property
        // filter; otherwise it would appear to "disappear" — refetch instead.
        const writeScope = resolvePropertyIdForWrite(subPropertyValue);
        const viewingAll = subPropertyValue === "";
        const viewingShared = subPropertyValue === "shared";
        const createdMatchesView =
          viewingAll ||
          (viewingShared && created.property_id == null) ||
          (writeScope != null && created.property_id === writeScope);
        if (createdMatchesView) {
          setHotelImages((prev) => [toEditableImage(created), ...prev]);
        } else {
          await fetchHotelImages(propertyCode, subPropertyValue);
        }
      } else {
        await fetchHotelImages(propertyCode, subPropertyValue);
      }
      setNewImageTitle("");
      setNewImageUrl("");
      setNewImageSourceLabel("");
      setNewImageCategory("");
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image added");
    } catch (e) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to add image");
    } finally {
      setCreatingImage(false);
    }
  }

  function titleFromFilename(filename: string): string {
    const stem = String(filename || "").replace(/\.[^.]+$/, "");
    const words = stem.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
    // A camera name ("JCR_1690", "IMG_9966") is no title at all - leave the field empty.
    if (!words || looksLikeFileName(words)) return "";
    return words.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function clearNewImageFile() {
    setNewImageFile(null);
    if (newImageFilePreview) URL.revokeObjectURL(newImageFilePreview);
    setNewImageFilePreview("");
    if (newImageFileRef.current) newImageFileRef.current.value = "";
  }

  function onNewImageFileChange(event: ChangeEvent<HTMLInputElement>) {
    const f = event.target.files?.[0];
    if (!f) return;
    if (!ALLOWED_UPLOAD_TYPES.includes(f.type)) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Unsupported file type. Allowed: PNG, JPEG, WebP.");
      if (newImageFileRef.current) newImageFileRef.current.value = "";
      return;
    }
    if (f.size > MAX_UPLOAD_BYTES) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(`File is too large (${(f.size / (1024 * 1024)).toFixed(1)} MB). Max 10 MB.`);
      if (newImageFileRef.current) newImageFileRef.current.value = "";
      return;
    }
    setNewImageFile(f);
    if (newImageFilePreview) URL.revokeObjectURL(newImageFilePreview);
    setNewImageFilePreview(URL.createObjectURL(f));
    // Seed the title from the filename only when the admin hasn't typed one,
    // so we never silently overwrite an intentional title.
    if (!newImageTitle.trim()) {
      const derived = titleFromFilename(f.name);
      if (derived) setNewImageTitle(derived);
    }
    setHotelImagesStatus("idle");
    setHotelImagesMsg("");
  }

  async function uploadHotelImage() {
    const title = newImageTitle.trim();
    const sourceLabel = newImageSourceLabel.trim();
    if (!title) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Title is required");
      return;
    }
    if (looksLikeFileName(title)) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(FILE_NAME_TITLE_HINT);
      return;
    }
    if (!newImageFile) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Choose an image file to upload");
      return;
    }

    if (!newImageCategory) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Choose what the photo shows (Category) so the bot knows when to show it");
      return;
    }

    setUploadingImage(true);
    try {
      const fd = new FormData();
      fd.append("file", newImageFile);
      fd.append("title", title);
      fd.append("category", newImageCategory);
      if (sourceLabel) fd.append("source_label", sourceLabel);
      const writeScope = resolvePropertyIdForWrite(subPropertyValue);
      if (writeScope != null) {
        fd.append("property_id", String(writeScope));
      } else if (subPropertyValue === "shared") {
        fd.append("property_id", "shared");
      }
      const payload = await adminSend<HotelImageUpdateResponse>(
        "POST",
        "/content/hotel-images/upload",
        fd,
        propertyCode,
      );
      const created = payload?.image;
      if (created) {
        const viewingAll = subPropertyValue === "";
        const viewingShared = subPropertyValue === "shared";
        const createdMatchesView =
          viewingAll ||
          (viewingShared && created.property_id == null) ||
          (writeScope != null && created.property_id === writeScope);
        if (createdMatchesView) {
          setHotelImages((prev) => [toEditableImage(created), ...prev]);
        } else {
          await fetchHotelImages(propertyCode, subPropertyValue);
        }
      } else {
        await fetchHotelImages(propertyCode, subPropertyValue);
      }
      setNewImageTitle("");
      setNewImageSourceLabel("");
      setNewImageCategory("");
      clearNewImageFile();
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image uploaded");
    } catch (e) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to upload image");
    } finally {
      setUploadingImage(false);
    }
  }

  async function deleteHotelImage(imageId: number) {
    const confirmed = window.confirm("Delete this image permanently from DB?");
    if (!confirmed) return;

    setDeletingImageId(imageId);
    try {
      await adminSend<{ deleted?: boolean }>(
        "DELETE",
        `/content/hotel-images/${imageId}`,
        undefined,
        propertyCode,
      );
      setHotelImages((prev) => prev.filter((row) => row.id !== imageId));
      setImageRowMessages((prev) => {
        if (!(imageId in prev)) return prev;
        const next = { ...prev };
        delete next[imageId];
        return next;
      });
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image deleted");
    } catch (e) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to delete image");
    } finally {
      setDeletingImageId(null);
    }
  }

  async function runBulkPreview(file: File, sheetName?: string) {
    setBulkLoading(true);
    setBulkError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (sheetName) fd.append("sheet", sheetName);
      // Preview itself doesn't write; we still send property_id so the server
      // could (later) include it in the preview response or surface scope
      // warnings. Today it's accepted-and-ignored — harmless.
      const writeScope = resolvePropertyIdForWrite(subPropertyValue);
      if (writeScope != null) {
        fd.append("property_id", String(writeScope));
      } else if (subPropertyValue === "shared") {
        fd.append("property_id", "shared");
      }
      const payload = await adminSend<BulkPreviewResponse>(
        "POST",
        "/content/hotel-images/bulk/preview",
        fd,
        propertyCode,
      );
      setBulkPreview(payload);
      setBulkSheet(sheetName || payload.selected_sheet || (payload.sheet_names[0] || ""));
    } catch (e) {
      setBulkError(e instanceof Error ? e.message : "Failed to read file");
      setBulkFile(null);
      setBulkPreview(null);
    } finally {
      setBulkLoading(false);
    }
  }

  async function onBulkFileChange(event: ChangeEvent<HTMLInputElement>) {
    const f = event.target.files?.[0];
    if (!f) return;
    setBulkFile(f);
    setBulkResult(null);
    setBulkSheet("");
    await runBulkPreview(f);
    if (bulkFileInputRef.current) {
      bulkFileInputRef.current.value = "";
    }
  }

  async function onBulkSheetChange(name: string) {
    if (!bulkFile) return;
    setBulkSheet(name);
    await runBulkPreview(bulkFile, name);
  }

  async function confirmBulkUpload() {
    if (!bulkFile) return;
    setBulkCommitting(true);
    setBulkError("");
    try {
      const fd = new FormData();
      fd.append("file", bulkFile);
      if (bulkSheet) fd.append("sheet", bulkSheet);
      const writeScope = resolvePropertyIdForWrite(subPropertyValue);
      if (writeScope != null) {
        fd.append("property_id", String(writeScope));
      } else if (subPropertyValue === "shared") {
        fd.append("property_id", "shared");
      }
      const payload = await adminSend<BulkCommitResponse>(
        "POST",
        "/content/hotel-images/bulk",
        fd,
        propertyCode,
      );
      setBulkResult(payload);
      setBulkPreview(null);
      setBulkFile(null);
      await fetchHotelImages(propertyCode, subPropertyValue);
      setHotelImagesStatus("ok");
      setHotelImagesMsg(
        `Bulk upload: ${payload.totals.inserted} added, ${payload.totals.updated} updated, ${payload.totals.failed} skipped`,
      );
    } catch (e) {
      setBulkError(e instanceof Error ? e.message : "Bulk upload failed");
    } finally {
      setBulkCommitting(false);
    }
  }

  function cancelBulk() {
    setBulkFile(null);
    setBulkPreview(null);
    setBulkResult(null);
    setBulkSheet("");
    setBulkError("");
    if (bulkFileInputRef.current) {
      bulkFileInputRef.current.value = "";
    }
  }

  async function refreshHotelImageManager() {
    await fetchProperties();
    await fetchSubProperties(propertyCode);
    await fetchHotelImages(propertyCode, subPropertyValue);
  }

  const selectedJob = jobs.find((j) => j.job_id === selectedJobId);

  return (
    <div className="flex-1 overflow-auto p-6 min-h-0">
    <div className="max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Content Manager</h1>
        <p className="text-muted-foreground">
          Manage hotel images and review extracted content in one place.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Hotel Image Manager</CardTitle>
            <Button variant="ghost" size="sm" onClick={refreshHotelImageManager}>
              <RefreshCcw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="content-hotel-picker">Select Hotel</Label>
            {loadingProperties ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading hotels...
              </div>
            ) : (
              <select
                id="content-hotel-picker"
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                value={propertyCode}
                onChange={(event) => {
                  const nextCode = normalizePropertyCode(event.target.value);
                  if (!nextCode) return;
                  setPropertyCode(nextCode);
                  setActivePropertyCode(nextCode);
                }}
              >
                {propertyOptions.map((row) => (
                  <option key={row.code} value={row.code}>
                    {[row.code, row.name, row.city].filter(Boolean).join(" | ")}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* Sub-property picker. Only rendered when the active hotel actually
              has rows in new_bot_properties; for single-property hotels the
              UI stays exactly as it was. */}
          {loadingSubProperties ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading properties...
            </div>
          ) : subProperties.length > 0 ? (
            <div className="space-y-1.5">
              <Label htmlFor="content-subproperty-picker">Select Property</Label>
              <select
                id="content-subproperty-picker"
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                value={subPropertyValue}
                onChange={(event) => setSubPropertyValue(event.target.value)}
              >
                <option value="">All images (any property)</option>
                <option value="shared">Shared across all properties</option>
                {subProperties.map((p) => (
                  <option key={p.id} value={String(p.id)}>
                    {p.name}
                    {p.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                Pick a property to scope new image uploads (and the list below) to
                that property. Pick &quot;Shared&quot; to attach images to every
                property under this hotel.
              </p>
            </div>
          ) : null}

          <div className="rounded-md border p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-sm font-medium">Add New Image</div>
              <div className="inline-flex rounded-md border p-0.5">
                <button
                  type="button"
                  onClick={() => setNewImageMode("url")}
                  className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                    newImageMode === "url"
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Paste URL
                </button>
                <button
                  type="button"
                  onClick={() => setNewImageMode("upload")}
                  className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                    newImageMode === "upload"
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Upload file
                </button>
              </div>
            </div>

            {newImageMode === "url" ? (
              <>
                <div className="grid gap-2 md:grid-cols-3">
                  <div className="space-y-1">
                    <Label htmlFor="new-image-title">Title</Label>
                    <Input
                      id="new-image-title"
                      value={newImageTitle}
                      onChange={(event) => setNewImageTitle(event.target.value)}
                      placeholder="e.g. Poolside Lounge"
                    />
                    {newImageTitle.trim() && looksLikeFileName(newImageTitle) ? (
                      <p className="text-xs text-amber-700">{FILE_NAME_TITLE_HINT}</p>
                    ) : null}
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="new-image-url">Image URL</Label>
                    <Input
                      id="new-image-url"
                      value={newImageUrl}
                      onChange={(event) => setNewImageUrl(event.target.value)}
                      placeholder="https://..."
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="new-image-source-label">Label</Label>
                    <Input
                      id="new-image-source-label"
                      value={newImageSourceLabel}
                      onChange={(event) => setNewImageSourceLabel(event.target.value)}
                      placeholder="optional source label"
                    />
                  </div>
                </div>
                <div className="mt-2 space-y-1">
                  <Label htmlFor="new-image-category">What does the photo show? (Category)</Label>
                  <select
                    id="new-image-category"
                    className="h-9 w-full rounded-md border bg-background px-3 text-sm md:w-[420px]"
                    value={newImageCategory}
                    onChange={(event) => setNewImageCategory(event.target.value)}
                  >
                    <option value="">Choose a category…</option>
                    {IMAGE_CATEGORY_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mt-3">
                  <Button size="sm" onClick={createHotelImage} disabled={creatingImage}>
                    {creatingImage ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : null}
                    Add Image
                  </Button>
                </div>
              </>
            ) : (
              <>
                <p className="mb-3 text-xs text-muted-foreground">
                  Upload a PNG, JPEG, or WebP (max 10 MB). The file is stored in S3.
                  The <strong>Title</strong> is what the bot matches on in chat, and the
                  <strong> Category</strong> decides where it can appear: only
                  Exterior and Room photos make up the gallery shown when a guest picks the
                  hotel.
                </p>
                <input
                  ref={newImageFileRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={onNewImageFileChange}
                />
                <div className="grid gap-3 md:grid-cols-[180px_1fr]">
                  <div className="self-start rounded-md border bg-muted p-2">
                    {newImageFilePreview ? (
                      <img
                        src={newImageFilePreview}
                        alt="Selected file preview"
                        className="block h-auto w-full rounded-sm object-contain"
                      />
                    ) : (
                      <button
                        type="button"
                        onClick={() => newImageFileRef.current?.click()}
                        className="flex h-32 w-full flex-col items-center justify-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      >
                        <Upload className="h-5 w-5" />
                        Choose image
                      </button>
                    )}
                  </div>
                  <div className="space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => newImageFileRef.current?.click()}
                        disabled={uploadingImage}
                      >
                        <Upload className="mr-1 h-4 w-4" />
                        {newImageFile ? "Change file" : "Choose file"}
                      </Button>
                      {newImageFile ? (
                        <>
                          <span className="max-w-[220px] truncate text-xs text-muted-foreground">
                            {newImageFile.name} ({(newImageFile.size / (1024 * 1024)).toFixed(1)} MB)
                          </span>
                          <button
                            type="button"
                            className="text-xs text-muted-foreground underline"
                            onClick={clearNewImageFile}
                            disabled={uploadingImage}
                          >
                            Remove
                          </button>
                        </>
                      ) : null}
                    </div>
                    <div className="space-y-1">
                      <Label htmlFor="new-image-upload-title">Title</Label>
                      <Input
                        id="new-image-upload-title"
                        value={newImageTitle}
                        onChange={(event) => setNewImageTitle(event.target.value)}
                        placeholder="e.g. Poolside Lounge"
                      />
                      {newImageTitle.trim() && looksLikeFileName(newImageTitle) ? (
                        <p className="text-xs text-amber-700">{FILE_NAME_TITLE_HINT}</p>
                      ) : null}
                    </div>
                    <div className="space-y-1">
                      <Label htmlFor="new-image-upload-category">What does the photo show? (Category)</Label>
                      <select
                        id="new-image-upload-category"
                        className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                        value={newImageCategory}
                        onChange={(event) => setNewImageCategory(event.target.value)}
                      >
                        <option value="">Choose a category…</option>
                        {IMAGE_CATEGORY_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <Label htmlFor="new-image-upload-source-label">Label</Label>
                      <Input
                        id="new-image-upload-source-label"
                        value={newImageSourceLabel}
                        onChange={(event) => setNewImageSourceLabel(event.target.value)}
                        placeholder="optional source label"
                      />
                    </div>
                    <div>
                      <Button
                        size="sm"
                        onClick={uploadHotelImage}
                        disabled={uploadingImage || !newImageFile}
                      >
                        {uploadingImage ? (
                          <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                        ) : null}
                        Upload Image
                      </Button>
                    </div>
                  </div>
                </div>
              </>
            )}
          </div>

          <div className="rounded-md border p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-sm font-medium">Bulk Upload (CSV/Excel)</div>
              <a
                href="/admin/api/content/hotel-images/bulk/template"
                download
                className="text-xs text-primary underline-offset-2 hover:underline"
              >
                Download sample template
              </a>
            </div>

            <div className="mb-3 rounded-md border-2 border-dashed border-primary/40 bg-primary/5 px-3 py-2 text-sm">
              <span className="text-muted-foreground">Uploading to: </span>
              <span className="font-bold">
                {(() => {
                  const active = propertyOptions.find((p) => p.code === propertyCode);
                  if (!active) return propertyCode || "default";
                  return [active.name, active.city].filter(Boolean).join(" — ") || active.code;
                })()}
              </span>
              <span className="ml-2 text-xs text-muted-foreground">[{propertyCode}]</span>
              {subProperties.length > 0 ? (
                <span className="ml-2 text-xs">
                  →{" "}
                  <span className="font-semibold">
                    {(() => {
                      if (subPropertyValue === "" || subPropertyValue === "shared") {
                        return "Shared (all properties)";
                      }
                      const pid = Number(subPropertyValue);
                      const match = subProperties.find((p) => p.id === pid);
                      return match ? match.name : `property #${pid}`;
                    })()}
                  </span>
                </span>
              ) : null}
            </div>

            <p className="mb-3 text-xs text-muted-foreground">
              The first 3 columns are read: <strong>column 1 = ignored</strong>{" "}
              (row number / index — pass anything),{" "}
              <strong>column 2 = label</strong> (what the photo shows — the bot matches on it),{" "}
              <strong>column 3 = image URL</strong>. <strong>Column 4 = category</strong> is
              read only when its header says <code>category</code> (see the sample: facade, room,
              room_detail, dining, pool, spa, banquet, lobby, fitness, kids, other); otherwise
              further columns are dropped. If a label already exists for this
              hotel
              {subProperties.length > 0 ? " and property scope" : ""} its image URL
              will be updated; otherwise a new image is added.
            </p>

            <input
              ref={bulkFileInputRef}
              type="file"
              accept=".csv,.xlsx,.xls,.tsv,.txt"
              className="hidden"
              onChange={onBulkFileChange}
            />

            {!bulkPreview && !bulkResult ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => bulkFileInputRef.current?.click()}
                  disabled={bulkLoading}
                >
                  {bulkLoading ? (
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  ) : (
                    <Upload className="mr-1 h-4 w-4" />
                  )}
                  Choose CSV / Excel file
                </Button>
                {bulkError ? (
                  <span className="text-xs text-red-600">{bulkError}</span>
                ) : null}
              </div>
            ) : null}

            {bulkPreview ? (
              <div className="space-y-3">
                <div className="text-xs text-muted-foreground">
                  File:{" "}
                  <span className="font-medium text-foreground">
                    {bulkPreview.filename || "(unnamed)"}
                  </span>
                  {bulkPreview.header_skipped ? (
                    <span className="ml-2">(header row skipped)</span>
                  ) : null}
                </div>

                {bulkPreview.sheet_names.length > 1 ? (
                  <div className="space-y-1">
                    <Label htmlFor="bulk-sheet-picker">Sheet</Label>
                    <select
                      id="bulk-sheet-picker"
                      className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                      value={bulkSheet || bulkPreview.sheet_names[0]}
                      onChange={(event) => onBulkSheetChange(event.target.value)}
                      disabled={bulkLoading || bulkCommitting}
                    >
                      {bulkPreview.sheet_names.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}

                <div className="text-sm">
                  <span className="font-medium text-green-700">
                    {bulkPreview.totals.valid}
                  </span>{" "}
                  valid row{bulkPreview.totals.valid === 1 ? "" : "s"}
                  {" · "}
                  <span className="font-medium text-amber-700">
                    {bulkPreview.totals.failed}
                  </span>{" "}
                  will be skipped
                </div>

                {bulkPreview.preview.length > 0 ? (
                  <div className="overflow-hidden rounded-md border">
                    <div className="border-b bg-muted px-3 py-2 text-xs font-medium">
                      Preview (first {bulkPreview.preview.length} of{" "}
                      {bulkPreview.totals.valid})
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="text-muted-foreground">
                          <tr>
                            <th className="px-3 py-1.5 text-left font-medium">Row</th>
                            <th className="px-3 py-1.5 text-left font-medium">Label</th>
                            <th className="px-3 py-1.5 text-left font-medium">Image URL</th>
                            <th className="px-3 py-1.5 text-left font-medium">Category</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bulkPreview.preview.map((r) => (
                            <tr key={`${r.row}-${r.label}`} className="border-t">
                              <td className="px-3 py-1.5">{r.row}</td>
                              <td className="px-3 py-1.5">
                                {r.label}
                                {looksLikeFileName(r.label) ? (
                                  <span className="ml-1 text-amber-700">(file name — will not match)</span>
                                ) : null}
                              </td>
                              <td className="px-3 py-1.5">
                                <span className="block max-w-[320px] truncate" title={r.image_url}>
                                  {r.image_url}
                                </span>
                              </td>
                              <td className="px-3 py-1.5">
                                {r.category ? (
                                  imageCategoryLabel(r.category)
                                ) : (
                                  <span className="text-amber-700">none</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ) : (
                  <div className="text-xs text-muted-foreground">
                    No valid rows found in this file.
                  </div>
                )}

                {bulkPreview.failed.length > 0 ? (
                  <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span>
                        {bulkPreview.failed.length} row
                        {bulkPreview.failed.length === 1 ? "" : "s"} will be skipped
                        (empty label, bad URL, or duplicate)
                      </span>
                      <button
                        type="button"
                        className="underline"
                        onClick={() =>
                          downloadFailedRowsCsv(bulkPreview.failed, bulkPreview.filename)
                        }
                      >
                        Download skipped rows
                      </button>
                    </div>
                  </div>
                ) : null}

                {bulkError ? (
                  <div className="text-xs text-red-600">{bulkError}</div>
                ) : null}

                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    onClick={confirmBulkUpload}
                    disabled={bulkCommitting || bulkPreview.totals.valid === 0}
                  >
                    {bulkCommitting ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : null}
                    Confirm upload ({bulkPreview.totals.valid})
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={cancelBulk}
                    disabled={bulkCommitting}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : null}

            {bulkResult ? (
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm font-medium text-green-700">
                  <CheckCircle2 className="h-4 w-4" />
                  Bulk upload complete: {bulkResult.totals.inserted} added,{" "}
                  {bulkResult.totals.updated} updated, {bulkResult.totals.failed} skipped
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {bulkResult.failed.length > 0 ? (
                    <button
                      type="button"
                      className="text-xs text-primary underline"
                      onClick={() =>
                        downloadFailedRowsCsv(bulkResult.failed, bulkResult.filename)
                      }
                    >
                      Download skipped rows
                    </button>
                  ) : null}
                  <Button size="sm" variant="outline" onClick={cancelBulk}>
                    Done
                  </Button>
                </div>
              </div>
            ) : null}
          </div>

          {loadingHotelImages ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading images...
            </div>
          ) : hotelImages.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No images found for this hotel.
            </p>
          ) : (
            <>
            {(() => {
              const missing = hotelImages.filter((r) => !r.category).length;
              return (
                <div
                  className={`flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs ${
                    missing ? "border-amber-400 bg-amber-50 text-amber-900" : "text-muted-foreground"
                  }`}
                >
                  <span>
                    {missing
                      ? `${missing} of ${hotelImages.length} photos have no category — the bot never shows them when a guest picks the hotel.`
                      : `All ${hotelImages.length} photos have a category.`}
                  </span>
                  {missing || showUncategorisedOnly ? (
                    <label className="inline-flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={showUncategorisedOnly}
                        onChange={(event) => setShowUncategorisedOnly(event.target.checked)}
                      />
                      Show only photos without a category
                    </label>
                  ) : null}
                </div>
              );
            })()}
            <ul className="space-y-3">
              {hotelImages
                .filter((row) => !showUncategorisedOnly || !row.category)
                .map((row) => (
                <li key={row.id} className="rounded-lg border p-3">
                  <div className="grid items-start gap-3 md:grid-cols-[220px_1fr]">
                    <div className="self-start rounded-md border bg-muted p-2">
                      {row.draftImageUrl ? (
                        <button
                          type="button"
                          onClick={() =>
                            setPreviewImage({
                              url: row.draftImageUrl,
                              title: row.draftTitle || `Hotel image ${row.id}`,
                            })
                          }
                          className="block w-full"
                        >
                          <img
                            src={row.draftImageUrl}
                            alt={row.draftTitle || `Hotel image ${row.id}`}
                            className="block h-auto w-full cursor-zoom-in rounded-sm object-contain"
                            loading="lazy"
                          />
                        </button>
                      ) : (
                        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
                          No image URL
                        </div>
                      )}
                    </div>
                    <div className="space-y-2">
                      <div className="space-y-1">
                        <Label htmlFor={`image-title-${row.id}`}>Title</Label>
                        <Input
                          id={`image-title-${row.id}`}
                          value={row.draftTitle}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftTitle", event.target.value)
                          }
                        />
                        {row.draftTitle.trim() && looksLikeFileName(row.draftTitle) ? (
                          <p className="text-xs text-amber-700">{FILE_NAME_TITLE_HINT}</p>
                        ) : null}
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor={`image-url-${row.id}`}>
                          {row.is_uploaded ? "Image URL (uploaded — read-only)" : "Image URL"}
                        </Label>
                        <Input
                          id={`image-url-${row.id}`}
                          value={row.draftImageUrl}
                          readOnly={row.is_uploaded}
                          disabled={row.is_uploaded}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftImageUrl", event.target.value)
                          }
                        />
                        {row.is_uploaded ? (
                          <p className="text-xs text-muted-foreground">
                            Stored in S3. The link is a temporary signed URL and
                            refreshes automatically — edit the Title to change the
                            keyword the bot matches on.
                          </p>
                        ) : null}
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor={`image-category-${row.id}`}>Category</Label>
                        <select
                          id={`image-category-${row.id}`}
                          className={`h-9 w-full rounded-md border bg-background px-3 text-sm ${
                            row.draftCategory ? "" : "border-amber-500"
                          }`}
                          value={row.draftCategory}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftCategory", event.target.value)
                          }
                        >
                          <option value="">
                            No category — never shown when a guest picks the hotel
                          </option>
                          {IMAGE_CATEGORY_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor={`image-source-${row.id}`}>Label</Label>
                        <Input
                          id={`image-source-${row.id}`}
                          value={row.draftSourceLabel}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftSourceLabel", event.target.value)
                          }
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <span>ID #{row.id}</span>
                        {row.category ? <span>Category: {imageCategoryLabel(row.category)}</span> : null}
                        {row.source_label ? <span>Source: {row.source_label}</span> : null}
                        {subProperties.length > 0 ? (
                          <span>
                            Scope:{" "}
                            <span className="font-medium">
                              {row.property_id == null
                                ? "Shared"
                                : subProperties.find((p) => p.id === row.property_id)?.name ||
                                  `#${row.property_id}`}
                            </span>
                          </span>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          onClick={() => saveHotelImage(row)}
                          disabled={!row.dirty || savingImageId === row.id || deletingImageId === row.id}
                        >
                          {savingImageId === row.id ? (
                            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                          ) : null}
                          Save
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => deleteHotelImage(row.id)}
                          disabled={savingImageId === row.id || deletingImageId === row.id}
                        >
                          {deletingImageId === row.id ? (
                            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="mr-1 h-4 w-4" />
                          )}
                          Delete
                        </Button>
                        {imageRowMessages[row.id] ? (
                          <span
                            className={`text-xs ${
                              imageRowMessages[row.id].type === "ok"
                                ? "text-green-600"
                                : "text-red-600"
                            }`}
                          >
                            {imageRowMessages[row.id].text}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
            </>
          )}

          {hotelImagesStatus === "err" ? (
            <div className="flex items-center gap-2 text-sm text-red-600">
              <AlertTriangle className="h-4 w-4" />
              {hotelImagesMsg}
            </div>
          ) : hotelImagesStatus === "ok" ? (
            <div className="flex items-center gap-2 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {hotelImagesMsg}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Select a Crawl Session to Review</CardTitle>
            <Button variant="ghost" size="sm" onClick={fetchJobs}>
              <RefreshCcw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {loadingJobs ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading sessions...
            </div>
          ) : jobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No reviewable sessions yet. Start a crawl in Web Crawling first.
            </p>
          ) : (
            <ul className="space-y-2">
              {jobs.map((job) => (
                <li key={job.job_id}>
                  <button
                    className={`w-full rounded-lg border px-4 py-3 text-left transition-colors ${
                      selectedJobId === job.job_id
                        ? "border-primary bg-primary/5"
                        : "hover:bg-muted/50"
                    }`}
                    onClick={() => loadReview(job.job_id)}
                  >
                    <div className="text-sm font-medium">
                      {job.session_name || "Unnamed Session"}
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {job.url} | {statusLabel(job.status)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {selectedJobId && (
        <div>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">
              Review: {selectedJob?.session_name || selectedJobId.slice(0, 8)}
            </h2>
            {selectedJob?.can_download && (
              <Button variant="outline" size="sm" asChild>
                <a href={scraperApi.downloadUrl(selectedJobId)} download>
                  <Download className="mr-1 h-3.5 w-3.5" />
                  Download ZIP
                </a>
              </Button>
            )}
          </div>

          {loadingReview ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading review data...
            </div>
          ) : reviewPayload ? (
            <>
              {reviewPayload.entities.map((entity) => (
                <EntityBlock
                  key={entity.id}
                  entity={entity}
                  onChange={updateEntity}
                />
              ))}

              <div className="mt-4 flex items-center gap-3">
                <Button
                  onClick={handlePublish}
                  disabled={publishStatus === "busy"}
                >
                  {publishStatus === "busy" ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : null}
                  Publish to Knowledge Base
                </Button>

                {publishStatus === "ok" && (
                  <div className="flex items-center gap-1.5 text-sm text-green-600">
                    <CheckCircle2 className="h-4 w-4" />
                    {publishMsg}
                  </div>
                )}
                {publishStatus === "err" && (
                  <div className="flex items-center gap-1.5 text-sm text-red-600">
                    <AlertTriangle className="h-4 w-4" />
                    {publishMsg}
                  </div>
                )}
              </div>
            </>
          ) : publishStatus === "err" ? (
            <div className="flex items-center gap-2 text-sm text-red-600">
              <AlertTriangle className="h-4 w-4" />
              {publishMsg}
            </div>
          ) : null}
        </div>
      )}

      {previewImage ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setPreviewImage(null)}
        >
          <div
            className="w-full max-w-5xl rounded-lg bg-background p-4 shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="truncate text-sm font-medium">{previewImage.title}</div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setPreviewImage(null)}
              >
                Back
              </Button>
            </div>
            <div className="max-h-[75vh] overflow-auto rounded-md border bg-muted p-2">
              <img
                src={previewImage.url}
                alt={previewImage.title}
                className="mx-auto h-auto max-h-[72vh] w-auto object-contain"
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
    </div>
  );
}
