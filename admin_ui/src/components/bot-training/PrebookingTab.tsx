import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ArrowDown, ArrowUp, ExternalLink, Loader2, Plus, Save, Trash2, Upload } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend, joinLines, splitLines } from "@/lib/adminApi";

// Everything a pre-booking hotel customises, saved in the DB per hotel
// (services/prebooking_settings_service.py, new_bot_location_intros). A hotel
// that has never saved these keeps its old behaviour, so the first save asks
// for confirmation.

type WelcomeButton = { label: string; action: "message" | "intro"; message: string };

type ServiceButton = {
  label: string;
  action: "form" | "link";
  url?: string;
  property_urls?: Record<string, string>;
};

type PrebookingSettings = {
  welcome_buttons: WelcomeButton[];
  after_lead_form: "intro" | "buttons";
  service_buttons: Record<string, ServiceButton[]>;
  service_priority?: Record<string, string>;
  chips_enabled: boolean;
  form_cancel_button: boolean;
  images_muted_services: string[];
  reply_style: {
    bullet: string;
    emoji: string;
    no_trailing_offer: boolean;
    enforce_language: boolean;
    give_number_when_cannot_arrange: boolean;
  };
  outside_recommendations: boolean;
  properties?: Record<string, unknown>;
  menus?: MenuEntry[];
  // KB book slugs; an answer from one of these books carries the menus.
  menu_topics?: string[];
};

type MenuEntry = { id: string; title: string; property_slug: string; file_name?: string };

type KbBook = { slug: string; title: string; properties: string[] };

type TicketingSettings = {
  widget_id: string;
  group_id: string;
  allowed_departments: string[];
  properties: Record<string, { fms_entity_id?: string }>;
};

type PropertyRow = {
  id: number;
  slug: string;
  name: string;
  location_label: string;
  city: string;
  aliases: string[];
};

type LocationGroup = {
  location_id: string;
  label: string;
  property_slugs: string[];
  property_names: string[];
  message: string;
};

type ServiceRow = { service_id: string; name: string };

type LoadResponse = {
  hotel_code: string;
  saved: boolean;
  settings: PrebookingSettings;
  ticketing_saved: boolean;
  ticketing: TicketingSettings;
  properties: PropertyRow[];
  locations: LocationGroup[];
  property_messages: Record<string, string>;
  services: ServiceRow[];
  kb_books?: KbBook[];
};

type PropertyForm = {
  id: number;
  slug: string;
  name: string;
  location_label: string;
  city: string;
  aliasesText: string;
  fms_entity_id: string;
};

const MAX_WELCOME_BUTTONS = 6;
const MAX_SERVICE_BUTTONS = 3;

interface PrebookingTabProps {
  propertyCode: string;
}

const errorText = (error: unknown) => String(error instanceof Error ? error.message : error);

// Same test the chat uses to recognise the room-booking service.
const looksLikeRoomBooking = (serviceId: string) => {
  const sid = serviceId.toLowerCase();
  if (/(support|complaint|callback|event|banquet|venue|spa|dining|table|transfer|cab|taxi)/.test(sid)) return false;
  return /(room|stay)/.test(sid) && /(book|reserv)/.test(sid);
};

// Older saves stored one label per service and no welcome action.
const normalizeLoaded = (raw: PrebookingSettings): PrebookingSettings => {
  const serviceButtons: Record<string, ServiceButton[]> = {};
  for (const [serviceId, value] of Object.entries(raw.service_buttons || {})) {
    const list = Array.isArray(value) ? value : value ? [value as unknown as ServiceButton] : [];
    serviceButtons[serviceId] = list.map((b) => ({
      label: String(b?.label || ""),
      action: b?.action === "link" ? "link" : "form",
      url: String(b?.url || ""),
      property_urls: { ...(b?.property_urls || {}) },
    }));
  }
  return {
    ...raw,
    after_lead_form: raw.after_lead_form === "buttons" ? "buttons" : "intro",
    welcome_buttons: (raw.welcome_buttons || []).map((b) => ({
      label: String(b?.label || ""),
      action: b?.action === "intro" ? "intro" : "message",
      message: String(b?.message || ""),
    })),
    service_buttons: serviceButtons,
    service_priority: { ...(raw.service_priority || {}) },
  };
};

const PrebookingTab = ({ propertyCode }: PrebookingTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);
  const [savingMessages, setSavingMessages] = useState(false);
  const [savingTicketing, setSavingTicketing] = useState(false);
  const [savingPropertyId, setSavingPropertyId] = useState<number | null>(null);
  const [data, setData] = useState<LoadResponse | null>(null);
  const [settings, setSettings] = useState<PrebookingSettings | null>(null);
  const [locationMessages, setLocationMessages] = useState<Record<string, string>>({});
  const [propertyMessages, setPropertyMessages] = useState<Record<string, string>>({});
  const [widgetId, setWidgetId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [departmentsText, setDepartmentsText] = useState("");
  const [propertyForms, setPropertyForms] = useState<PropertyForm[]>([]);
  const [menuTitle, setMenuTitle] = useState("");
  const [menuProperty, setMenuProperty] = useState("__all__");
  const [menuFile, setMenuFile] = useState<File | null>(null);
  const [menuInputKey, setMenuInputKey] = useState(0);
  const [uploadingMenu, setUploadingMenu] = useState(false);
  const [deletingMenuId, setDeletingMenuId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const payload = await adminGet<LoadResponse>("/config/prebooking", propertyCode);
      setData(payload);
      setSettings(normalizeLoaded(payload.settings));
      setLocationMessages(Object.fromEntries((payload.locations || []).map((g) => [g.location_id, g.message || ""])));
      setPropertyMessages({ ...(payload.property_messages || {}) });
      setWidgetId(payload.ticketing.widget_id || "");
      setGroupId(payload.ticketing.group_id || "");
      setDepartmentsText(joinLines(payload.ticketing.allowed_departments));
      setPropertyForms(
        payload.properties.map((row) => ({
          id: row.id,
          slug: row.slug,
          name: row.name,
          location_label: row.location_label,
          city: row.city,
          aliasesText: row.aliases.join(", "),
          fms_entity_id: payload.ticketing.properties?.[row.slug]?.fms_entity_id || "",
        })),
      );
    } catch (error) {
      setData(null);
      setSettings(null);
      setLoadError(errorText(error));
      toast({ title: "Failed to load pre-booking settings", description: errorText(error), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [propertyCode, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const update = (patch: Partial<PrebookingSettings>) => setSettings((prev) => (prev ? { ...prev, ...patch } : prev));

  const updateStyle = (patch: Partial<PrebookingSettings["reply_style"]>) =>
    setSettings((prev) => (prev ? { ...prev, reply_style: { ...prev.reply_style, ...patch } } : prev));

  // ---- opening buttons ----
  const updateWelcome = (index: number, patch: Partial<WelcomeButton>) =>
    setSettings((prev) =>
      prev
        ? { ...prev, welcome_buttons: prev.welcome_buttons.map((row, i) => (i === index ? { ...row, ...patch } : row)) }
        : prev,
    );

  const moveWelcome = (index: number, delta: number) =>
    setSettings((prev) => {
      if (!prev) return prev;
      const target = index + delta;
      if (target < 0 || target >= prev.welcome_buttons.length) return prev;
      const next = [...prev.welcome_buttons];
      [next[index], next[target]] = [next[target], next[index]];
      return { ...prev, welcome_buttons: next };
    });

  const removeWelcome = (index: number) =>
    setSettings((prev) => (prev ? { ...prev, welcome_buttons: prev.welcome_buttons.filter((_, i) => i !== index) } : prev));

  const addWelcome = () =>
    setSettings((prev) =>
      prev && prev.welcome_buttons.length < MAX_WELCOME_BUTTONS
        ? { ...prev, welcome_buttons: [...prev.welcome_buttons, { label: "", action: "message", message: "" }] }
        : prev,
    );

  // ---- service buttons ----
  const setServiceButtons = (serviceId: string, buttons: ServiceButton[]) =>
    setSettings((prev) => {
      if (!prev) return prev;
      const next = { ...prev.service_buttons };
      if (buttons.length) next[serviceId] = buttons;
      else delete next[serviceId];
      return { ...prev, service_buttons: next };
    });

  const updateServiceButton = (serviceId: string, index: number, patch: Partial<ServiceButton>) => {
    const current = settings?.service_buttons[serviceId] || [];
    setServiceButtons(
      serviceId,
      current.map((b, i) => (i === index ? { ...b, ...patch } : b)),
    );
  };

  const addServiceButton = (serviceId: string) => {
    const current = settings?.service_buttons[serviceId] || [];
    if (current.length >= MAX_SERVICE_BUTTONS) return;
    const hasForm = current.some((b) => b.action === "form");
    setServiceButtons(serviceId, [
      ...current,
      { label: "", action: hasForm ? "link" : "form", url: "", property_urls: {} },
    ]);
  };

  const removeServiceButton = (serviceId: string, index: number) => {
    const current = settings?.service_buttons[serviceId] || [];
    setServiceButtons(
      serviceId,
      current.filter((_, i) => i !== index),
    );
  };

  const setServicePriority = (serviceId: string, level: string) =>
    setSettings((prev) => {
      if (!prev) return prev;
      const next = { ...(prev.service_priority || {}) };
      if (level === "default") delete next[serviceId];
      else next[serviceId] = level;
      return { ...prev, service_priority: next };
    });

  const toggleMuted = (serviceId: string, muted: boolean) =>
    setSettings((prev) => {
      if (!prev) return prev;
      const rest = prev.images_muted_services.filter((id) => id !== serviceId);
      return { ...prev, images_muted_services: muted ? [...rest, serviceId] : rest };
    });

  const confirmFirstSave = (alreadySaved: boolean, what: string) =>
    alreadySaved ||
    window.confirm(
      `This hotel does not use admin-managed ${what} yet. Saving switches it from its current behaviour to these settings. Continue?`,
    );

  // Each save refreshes only what it saved, so unsaved edits elsewhere on the
  // page (another property, the ticket routing) are kept.
  const saveSettings = async () => {
    if (!settings || !data || !confirmFirstSave(data.saved, "pre-booking settings")) return;
    setSavingSettings(true);
    try {
      const { properties: _ignored, ...rest } = settings;
      const result = await adminSend<{ settings: PrebookingSettings }>(
        "PUT",
        "/config/prebooking",
        { settings: rest },
        propertyCode,
      );
      setSettings(normalizeLoaded(result.settings));
      setData((prev) => (prev ? { ...prev, saved: true, settings: result.settings } : prev));
      toast({ title: "Pre-booking settings saved" });
    } catch (error) {
      toast({ title: "Could not save pre-booking settings", description: errorText(error), variant: "destructive" });
    } finally {
      setSavingSettings(false);
    }
  };

  const saveMessages = async () => {
    setSavingMessages(true);
    try {
      const result = await adminSend<{ locations: Record<string, string>; properties: Record<string, string> }>(
        "PUT",
        "/config/prebooking/messages",
        { locations: locationMessages, properties: propertyMessages },
        propertyCode,
      );
      setLocationMessages(result.locations || {});
      setPropertyMessages(result.properties || {});
      toast({ title: "Messages saved", description: "They show in the chat straight away." });
    } catch (error) {
      toast({ title: "Could not save the messages", description: errorText(error), variant: "destructive" });
    } finally {
      setSavingMessages(false);
    }
  };

  const saveTicketing = async () => {
    if (!data || !confirmFirstSave(data.ticketing_saved, "ticket routing")) return;
    setSavingTicketing(true);
    try {
      const result = await adminSend<{ ticketing: TicketingSettings }>(
        "PUT",
        "/config/ticketing",
        { settings: { widget_id: widgetId, group_id: groupId, allowed_departments: splitLines(departmentsText) } },
        propertyCode,
      );
      setWidgetId(result.ticketing.widget_id || "");
      setGroupId(result.ticketing.group_id || "");
      setDepartmentsText(joinLines(result.ticketing.allowed_departments));
      setData((prev) => (prev ? { ...prev, ticketing_saved: true, ticketing: result.ticketing } : prev));
      toast({ title: "Ticket routing saved" });
    } catch (error) {
      toast({ title: "Could not save ticket routing", description: errorText(error), variant: "destructive" });
    } finally {
      setSavingTicketing(false);
    }
  };

  const setMenus = (menus: MenuEntry[]) => setSettings((prev) => (prev ? { ...prev, menus } : prev));

  const toggleMenuTopic = (slug: string, on: boolean) =>
    setSettings((prev) => {
      if (!prev) return prev;
      const rest = (prev.menu_topics || []).filter((s) => s !== slug);
      return { ...prev, menu_topics: on ? [...rest, slug] : rest };
    });

  const uploadMenu = async () => {
    if (!menuFile || !menuTitle.trim()) {
      toast({ title: "Add a title and choose a PDF first", variant: "destructive" });
      return;
    }
    setUploadingMenu(true);
    try {
      const form = new FormData();
      form.append("file", menuFile);
      form.append("title", menuTitle.trim());
      form.append("property_slug", menuProperty === "__all__" ? "" : menuProperty);
      const result = await adminSend<{ menus: MenuEntry[] }>("POST", "/config/prebooking/menus", form, propertyCode);
      setMenus(result.menus || []);
      setMenuTitle("");
      setMenuFile(null);
      setMenuInputKey((k) => k + 1);
      toast({ title: "Menu uploaded", description: "Guests who ask for the menu now get it." });
    } catch (error) {
      toast({ title: "Could not upload the menu", description: errorText(error), variant: "destructive" });
    } finally {
      setUploadingMenu(false);
    }
  };

  const deleteMenu = async (menu: MenuEntry) => {
    if (!window.confirm(`Remove "${menu.title}"? Guests will no longer receive it.`)) return;
    setDeletingMenuId(menu.id);
    try {
      const result = await adminSend<{ menus: MenuEntry[] }>(
        "DELETE",
        `/config/prebooking/menus/${encodeURIComponent(menu.id)}`,
        undefined,
        propertyCode,
      );
      setMenus(result.menus || []);
      toast({ title: "Menu removed" });
    } catch (error) {
      toast({ title: "Could not remove the menu", description: errorText(error), variant: "destructive" });
    } finally {
      setDeletingMenuId("");
    }
  };

  const updateProperty = (id: number, patch: Partial<PropertyForm>) =>
    setPropertyForms((prev) => prev.map((row) => (row.id === id ? { ...row, ...patch } : row)));

  const saveProperty = async (form: PropertyForm) => {
    setSavingPropertyId(form.id);
    try {
      const result = await adminSend<{
        property: { name: string; location_label: string; city: string; aliases: string[]; fms_entity_id: string };
      }>(
        "PUT",
        `/hotel-properties/${form.id}`,
        {
          name: form.name,
          location_label: form.location_label,
          city: form.city,
          aliases: form.aliasesText.split(",").map((entry) => entry.trim()).filter(Boolean),
          fms_entity_id: form.fms_entity_id,
        },
        propertyCode,
      );
      const saved = result.property;
      updateProperty(form.id, {
        name: saved.name,
        location_label: saved.location_label,
        city: saved.city,
        aliasesText: saved.aliases.join(", "),
        fms_entity_id: saved.fms_entity_id,
      });
      toast({
        title: `${saved.name} saved`,
        description: "If you changed a picker label, reload this tab to see the locations regrouped.",
      });
    } catch (error) {
      toast({ title: `Could not save ${form.name}`, description: errorText(error), variant: "destructive" });
    } finally {
      setSavingPropertyId(null);
    }
  };

  if (!loading && loadError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load pre-booking settings for {propertyCode}</AlertTitle>
        <AlertDescription>
          {loadError}. Pick a saved property above and press Load Property, then open this tab again.
        </AlertDescription>
      </Alert>
    );
  }

  if (loading || !data || !settings) {
    return (
      <div className="flex items-center py-8 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading pre-booking settings...
      </div>
    );
  }

  const services = data.services;
  const properties = data.properties;
  const multiProperty = properties.length > 1;
  // The hotel's shape decides the flow, and it follows from the picker labels
  // in section 6: properties with the same label are one location.
  const shape: "single" | "one_location" | "many_locations" = !multiProperty
    ? "single"
    : data.locations.length <= 1
      ? "one_location"
      : "many_locations";
  const shapeText = {
    single:
      "One hotel: there is no location picker. The guest sees the message below after the lead form, or when they tap an opening button set to show it.",
    one_location:
      "Several hotels in one location: there is no location picker. The guest sees the comparison below with one button per hotel; tapping a hotel shows that hotel's message and the chat stays on that hotel.",
    many_locations:
      "Hotels in several locations: the guest picks a location first (in the lead form or a dropdown) and sees that location's message. A location with several hotels shows one button per hotel.",
  }[shape];

  return (
    <div className="space-y-6">
      {!data.saved ? (
        <Alert>
          <AlertTitle>Not switched on for {data.hotel_code}</AlertTitle>
          <AlertDescription>
            This hotel still uses its current behaviour. The values below are the defaults; saving them switches the
            hotel to admin-managed pre-booking settings.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">How a guest moves through the chat</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li>The chat opens with the greeting and the lead form (fields are set in Admin &gt; Forms).</li>
            <li>
              After the form, or when the guest taps an opening button set to <em>Shows the saved message</em>, they
              see the saved message from section 2. Where several hotels share a location it compares them, with one
              button per hotel; with hotels in several cities the guest picks the city first.
            </li>
            <li>Tapping a hotel shows that hotel's saved message, and the chat then stays on that hotel.</li>
            <li>
              When a service answers, its buttons appear (section 3): a link (the chosen hotel's link) or the service's
              form, which raises a ticket for the chosen hotel.
            </li>
          </ol>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">1. Opening buttons</CardTitle>
          <CardDescription>
            Shown under the greeting before the guest has typed anything. A button either sends a message as if the
            guest typed it, or shows the saved message from section 2 (the hotel comparison when there are several).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_2fr]">
            <Label className="self-center">After the lead form is submitted</Label>
            <Select
              value={settings.after_lead_form}
              onValueChange={(v) => update({ after_lead_form: v === "buttons" ? "buttons" : "intro" })}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="intro">Show the saved message (section 2)</SelectItem>
                <SelectItem value="buttons">Show the opening buttons</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {settings.welcome_buttons.length === 0 ? (
            <p className="text-sm text-muted-foreground">No opening buttons.</p>
          ) : null}
          {settings.welcome_buttons.map((row, index) => (
            <div key={index} className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_1fr_2fr_auto]">
              <div className="space-y-1">
                <Label>Button label</Label>
                <Input value={row.label} maxLength={40} onChange={(e) => updateWelcome(index, { label: e.target.value })} />
              </div>
              <div className="space-y-1">
                <Label>What it does</Label>
                <Select
                  value={row.action}
                  onValueChange={(v) => updateWelcome(index, { action: v === "intro" ? "intro" : "message" })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="message">Sends a message</SelectItem>
                    <SelectItem value="intro">Shows the saved message (section 2)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label>{row.action === "message" ? "Message it sends" : "Message"}</Label>
                <Input
                  value={row.action === "message" ? row.message : ""}
                  placeholder={row.action === "message" ? "" : "Uses the saved message from section 2"}
                  disabled={row.action !== "message"}
                  maxLength={300}
                  onChange={(e) => updateWelcome(index, { message: e.target.value })}
                />
              </div>
              <div className="flex items-end gap-1">
                <Button type="button" variant="outline" size="icon" aria-label="Move up" onClick={() => moveWelcome(index, -1)}>
                  <ArrowUp className="h-4 w-4" />
                </Button>
                <Button type="button" variant="outline" size="icon" aria-label="Move down" onClick={() => moveWelcome(index, 1)}>
                  <ArrowDown className="h-4 w-4" />
                </Button>
                <Button type="button" variant="outline" size="icon" aria-label="Remove button" onClick={() => removeWelcome(index)}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            onClick={addWelcome}
            disabled={settings.welcome_buttons.length >= MAX_WELCOME_BUTTONS}
          >
            <Plus className="mr-2 h-4 w-4" /> Add button
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">2. Location and hotel messages</CardTitle>
          <CardDescription>
            Shown instantly, word for word. You can use **bold**, • bullets and ### headings. Leave a message empty
            and the bot answers that step itself.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <Alert>
            <AlertTitle>
              {shape === "single"
                ? "This hotel: one property"
                : shape === "one_location"
                  ? `This hotel: ${properties.length} properties in one location`
                  : `This hotel: ${properties.length} properties in ${data.locations.length} locations`}
            </AlertTitle>
            <AlertDescription>
              {shapeText} Properties with the same picker label (section 6) count as one location.
            </AlertDescription>
          </Alert>
          {data.locations.length === 0 ? (
            <p className="text-sm text-muted-foreground">No properties yet. Upload and reindex the KB in the RAG tab.</p>
          ) : null}
          {data.locations.map((group) => {
            const several = group.property_slugs.length > 1;
            return (
              <div key={group.location_id} className="space-y-3 rounded-md border p-3">
                <div className="text-sm font-medium">
                  {group.label}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {several ? `${group.property_slugs.length} hotels: ${group.property_names.join(", ")}` : "1 hotel"}
                  </span>
                </div>
                <div className="space-y-1">
                  <Label>
                    {several
                      ? "Comparison message (one button per hotel is added under it)"
                      : shape === "single"
                        ? "Opening message"
                        : "Message when the guest picks this location"}
                  </Label>
                  <Textarea
                    rows={several ? 8 : 5}
                    value={locationMessages[group.location_id] || ""}
                    placeholder={
                      several
                        ? "Compare the hotels in a few lines each and end by asking which one they would like."
                        : ""
                    }
                    onChange={(e) =>
                      setLocationMessages((prev) => ({ ...prev, [group.location_id]: e.target.value }))
                    }
                  />
                </div>
                {several
                  ? group.property_slugs.map((slug, i) => (
                      <div key={slug} className="space-y-1">
                        <Label>Message when the guest taps "{group.property_names[i]}"</Label>
                        <Textarea
                          rows={4}
                          value={propertyMessages[slug] || ""}
                          placeholder="Welcome them to this hotel and say what they can ask next."
                          onChange={(e) => setPropertyMessages((prev) => ({ ...prev, [slug]: e.target.value }))}
                        />
                      </div>
                    ))
                  : null}
              </div>
            );
          })}
          <div className="flex justify-end">
            <Button onClick={saveMessages} disabled={savingMessages || data.locations.length === 0}>
              {savingMessages ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
              Save messages
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">3. Service buttons</CardTitle>
          <CardDescription>
            Buttons shown with a service's answers. <strong>Opens the form</strong> opens that service's form (set in
            the Phases tab) and raises a ticket for the hotel the guest chose. <strong>Opens a link</strong> opens a
            link
            {multiProperty ? ", either one for every hotel or a different one per hotel" : ""}. Up to{" "}
            {MAX_SERVICE_BUTTONS} per service, one form button.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {services.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pre-booking services yet. Create them in the Phases tab.</p>
          ) : null}
          {services.map((service) => {
            const buttons = settings.service_buttons[service.service_id] || [];
            const room = looksLikeRoomBooking(service.service_id);
            return (
              <div key={service.service_id} className="space-y-3 rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium">
                    {service.name}
                    <span className="ml-2 text-xs font-normal text-muted-foreground">{service.service_id}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-4">
                    <div className="flex items-center gap-2 text-sm">
                      <span>Ticket priority</span>
                      <Select
                        value={settings.service_priority?.[service.service_id] || "default"}
                        onValueChange={(v) => setServicePriority(service.service_id, v)}
                      >
                        <SelectTrigger className="h-8 w-32">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="default">Default (Low)</SelectItem>
                          <SelectItem value="LOW">Low</SelectItem>
                          <SelectItem value="MEDIUM">Medium</SelectItem>
                          <SelectItem value="HIGH">High</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <label className="flex items-center gap-2 text-sm">
                      <Checkbox
                        checked={settings.images_muted_services.includes(service.service_id)}
                        onCheckedChange={(v) => toggleMuted(service.service_id, v === true)}
                      />
                      No photos with this service's answers
                    </label>
                  </div>
                </div>
                {room ? (
                  <p className="text-xs text-muted-foreground">
                    Room booking: the buttons appear when the guest is booking and has chosen a hotel. The form button
                    opens the team-help form (dates, guests, a note) and raises a booking ticket for that hotel. With
                    only a form button and no link, the team takes every booking.
                  </p>
                ) : null}
                {buttons.map((button, index) => (
                  <div key={index} className="space-y-2 rounded-md bg-muted/40 p-3">
                    <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                      <div className="space-y-1">
                        <Label>Button label</Label>
                        <Input
                          value={button.label}
                          maxLength={40}
                          placeholder={button.action === "form" ? "Get help from our team" : "Book on website"}
                          onChange={(e) => updateServiceButton(service.service_id, index, { label: e.target.value })}
                        />
                      </div>
                      <div className="space-y-1">
                        <Label>What it does</Label>
                        <Select
                          value={button.action}
                          onValueChange={(v) =>
                            updateServiceButton(service.service_id, index, { action: v === "link" ? "link" : "form" })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="form">Opens the form</SelectItem>
                            <SelectItem value="link">Opens a link</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="flex items-end">
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          aria-label="Remove button"
                          onClick={() => removeServiceButton(service.service_id, index)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                    {button.action === "link" ? (
                      <div className="grid gap-2 sm:grid-cols-2">
                        <div className="space-y-1 sm:col-span-2">
                          <Label>{multiProperty ? "Link for every hotel (optional when each hotel has its own)" : "Link"}</Label>
                          <Input
                            value={button.url || ""}
                            placeholder="https://..."
                            onChange={(e) => updateServiceButton(service.service_id, index, { url: e.target.value })}
                          />
                        </div>
                        {multiProperty
                          ? properties.map((prop) => (
                              <div key={prop.slug} className="space-y-1">
                                <Label>Link for {prop.name}</Label>
                                <Input
                                  value={button.property_urls?.[prop.slug] || ""}
                                  placeholder="https://... (empty = the link above)"
                                  onChange={(e) =>
                                    updateServiceButton(service.service_id, index, {
                                      property_urls: { ...(button.property_urls || {}), [prop.slug]: e.target.value },
                                    })
                                  }
                                />
                              </div>
                            ))
                          : null}
                      </div>
                    ) : null}
                  </div>
                ))}
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => addServiceButton(service.service_id)}
                  disabled={buttons.length >= MAX_SERVICE_BUTTONS}
                >
                  <Plus className="mr-2 h-4 w-4" /> Add button
                </Button>
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">4. Menus (PDF)</CardTitle>
          <CardDescription>
            Keep a few signature dishes in the KB; the full menu goes out as a PDF when a guest asks for the menu, or when
            the bot answers about one of the topics ticked below.
            {multiProperty ? " Each menu belongs to one hotel or to all of them; the guest gets the ones for the hotel they chose." : ""}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {(settings.menus || []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No menus uploaded.</p>
          ) : null}
          {(settings.menus || []).map((menu) => (
            <div key={menu.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
              <div className="text-sm">
                <div className="font-medium">{menu.title}</div>
                <div className="text-xs text-muted-foreground">
                  {menu.property_slug
                    ? properties.find((p) => p.slug === menu.property_slug)?.name || menu.property_slug
                    : "All hotels"}
                  {menu.file_name ? ` · ${menu.file_name}` : ""}
                </div>
              </div>
              <div className="flex gap-2">
                <Button asChild variant="outline" size="sm">
                  <a href={`/api/widget/menu/${data.hotel_code}/${menu.id}`} target="_blank" rel="noopener noreferrer">
                    <ExternalLink className="mr-2 h-4 w-4" /> Open
                  </a>
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => deleteMenu(menu)}
                  disabled={deletingMenuId === menu.id}
                >
                  {deletingMenuId === menu.id ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Trash2 className="mr-2 h-4 w-4" />
                  )}
                  Remove
                </Button>
              </div>
            </div>
          ))}
          {data.saved ? (
            <div className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_1fr_1fr_auto]">
              <div className="space-y-1">
                <Label>Title the guest sees</Label>
                <Input
                  value={menuTitle}
                  maxLength={80}
                  placeholder="e.g. Blend restaurant menu"
                  onChange={(e) => setMenuTitle(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label>Hotel</Label>
                <Select value={menuProperty} onValueChange={setMenuProperty}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">All hotels</SelectItem>
                    {properties.map((prop) => (
                      <SelectItem key={prop.slug} value={prop.slug}>
                        {prop.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label>PDF file</Label>
                <Input
                  key={menuInputKey}
                  type="file"
                  accept="application/pdf,.pdf"
                  onChange={(e) => setMenuFile(e.target.files?.[0] || null)}
                />
              </div>
              <div className="flex items-end">
                <Button onClick={uploadMenu} disabled={uploadingMenu}>
                  {uploadingMenu ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
                  Upload
                </Button>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Save the pre-booking settings once before uploading menus.</p>
          )}
          <div className="space-y-2 rounded-md border p-3">
            <Label>Show the menus without being asked</Label>
            <p className="text-xs text-muted-foreground">
              When a guest's question is about one of these knowledge-base topics, the bot adds the menus for the hotel
              being discussed{multiProperty ? " (every hotel's when the guest has not picked one)" : ""}. A general question
              that only touches the topic, like "tell me about the hotel", does not. Each menu goes out once per
              conversation; a guest who asks for the menu always gets it. Saved with "Save buttons and reply settings"
              below.
            </p>
            {(data.kb_books || []).length === 0 ? (
              <p className="text-sm text-muted-foreground">This hotel has no knowledge-base topics yet.</p>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {(data.kb_books || []).map((book) => (
                  <label key={book.slug} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={(settings.menu_topics || []).includes(book.slug)}
                      onCheckedChange={(v) => toggleMenuTopic(book.slug, v === true)}
                    />
                    <span>
                      {book.title}
                      {multiProperty && book.properties.length === 1 ? (
                        <span className="text-xs text-muted-foreground"> · {book.properties[0]}</span>
                      ) : null}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">5. Replies and behaviour</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <Label>Bullet style</Label>
            <Select value={settings.reply_style.bullet} onValueChange={(v) => updateStyle({ bullet: v })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="•">• (dot)</SelectItem>
                <SelectItem value="-">- (dash)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label>Emoji in replies</Label>
            <Select value={settings.reply_style.emoji} onValueChange={(v) => updateStyle({ emoji: v })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="limited">At most one</SelectItem>
                <SelectItem value="allowed">Allowed</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center justify-between">
            <Label>Suggestion chips</Label>
            <Switch checked={settings.chips_enabled} onCheckedChange={(v) => update({ chips_enabled: v })} />
          </div>
          <div className="flex items-center justify-between">
            <Label>"Cancel request" button on forms</Label>
            <Switch checked={settings.form_cancel_button} onCheckedChange={(v) => update({ form_cancel_button: v })} />
          </div>
          <div className="flex items-center justify-between">
            <Label>No "Let me know if..." sign-off</Label>
            <Switch
              checked={settings.reply_style.no_trailing_offer}
              onCheckedChange={(v) => updateStyle({ no_trailing_offer: v })}
            />
          </div>
          <div className="flex items-center justify-between">
            <Label>Always reply in the guest's language</Label>
            <Switch
              checked={settings.reply_style.enforce_language}
              onCheckedChange={(v) => updateStyle({ enforce_language: v })}
            />
          </div>
          <div className="flex items-center justify-between">
            <Label>Give the phone number when the hotel can't arrange something</Label>
            <Switch
              checked={settings.reply_style.give_number_when_cannot_arrange}
              onCheckedChange={(v) => updateStyle({ give_number_when_cannot_arrange: v })}
            />
          </div>
          <div className="flex items-center justify-between">
            <Label>Recommend places outside the hotel (web lookup)</Label>
            <Switch
              checked={settings.outside_recommendations}
              onCheckedChange={(v) => update({ outside_recommendations: v })}
            />
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={saveSettings} disabled={savingSettings}>
          {savingSettings ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
          {data.saved ? "Save buttons and reply settings" : "Switch on and save buttons and reply settings"}
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">6. Properties</CardTitle>
          <CardDescription>
            The name the bot says and shows on the hotel buttons, the picker label (properties with the same label form
            one location and are compared in one message), other names guests use, and the FMS entity ID for tickets.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {propertyForms.length === 0 ? (
            <p className="text-sm text-muted-foreground">No properties yet. Upload and reindex the KB in the RAG tab.</p>
          ) : null}
          {propertyForms.map((form) => (
            <div key={form.id} className="space-y-3 rounded-md border p-3">
              <div className="text-xs text-muted-foreground">{form.slug}</div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label>Name the bot says</Label>
                  <Input value={form.name} onChange={(e) => updateProperty(form.id, { name: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <Label>Picker label (location)</Label>
                  <Input
                    value={form.location_label}
                    onChange={(e) => updateProperty(form.id, { location_label: e.target.value })}
                  />
                </div>
                <div className="space-y-1">
                  <Label>City</Label>
                  <Input value={form.city} onChange={(e) => updateProperty(form.id, { city: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <Label>Other names guests use (comma-separated)</Label>
                  <Input
                    value={form.aliasesText}
                    onChange={(e) => updateProperty(form.id, { aliasesText: e.target.value })}
                  />
                </div>
                <div className="space-y-1">
                  <Label>FMS entity ID</Label>
                  <Input
                    value={form.fms_entity_id}
                    placeholder={data.ticketing_saved ? "e.g. 6586" : "Save ticket routing first"}
                    disabled={!data.ticketing_saved}
                    onChange={(e) => updateProperty(form.id, { fms_entity_id: e.target.value })}
                  />
                </div>
              </div>
              <div className="flex justify-end">
                <Button variant="outline" onClick={() => saveProperty(form)} disabled={savingPropertyId === form.id}>
                  {savingPropertyId === form.id ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="mr-2 h-4 w-4" />
                  )}
                  Save property
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {!data.ticketing_saved || propertyForms.some((f) => !f.fms_entity_id) ? (
        <Alert variant="destructive">
          <AlertTitle>Tickets are not reaching any dashboard yet</AlertTitle>
          <AlertDescription>
            {propertyForms.filter((f) => !f.fms_entity_id).map((f) => f.name).join(", ") || "This hotel"} has no FMS
            entity. Until one is set in section 6 (after saving section 7), tickets for it are kept in our system only
            and never sent to another hotel's dashboard. The entity, its departments and staff must first exist in the
            GHN ticketing dashboard.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">7. Ticket routing</CardTitle>
          <CardDescription>
            IDs from the GHN ticketing dashboard, and the department names tickets may be assigned to (one per line,
            spelled exactly as in the dashboard).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label>Widget ID</Label>
              <Input value={widgetId} onChange={(e) => setWidgetId(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label>Group ID</Label>
              <Input value={groupId} onChange={(e) => setGroupId(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1">
            <Label>Allowed departments</Label>
            <Textarea rows={4} value={departmentsText} onChange={(e) => setDepartmentsText(e.target.value)} />
          </div>
          <div className="flex justify-end">
            <Button onClick={saveTicketing} disabled={savingTicketing}>
              {savingTicketing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
              {data.ticketing_saved ? "Save ticket routing" : "Switch on and save"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default PrebookingTab;
