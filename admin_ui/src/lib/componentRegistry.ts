type ComponentMeta = {
  name: string;
  description: string;
  configurable: boolean;
  mobileAdaptive: boolean;
};

const _registry = new Map<string, ComponentMeta>();

export const componentRegistry = {
  register(key: string, meta: ComponentMeta): void {
    _registry.set(key, meta);
  },
  list(): Array<{ key: string } & ComponentMeta> {
    return Array.from(_registry.entries()).map(([key, meta]) => ({ key, ...meta }));
  },
  resolve(key: string): ComponentMeta | undefined {
    return _registry.get(key);
  },
};

componentRegistry.register("SuggestionChips", {
  name: "Suggestion Chips",
  description: "Clickable quick-action buttons shown above the input after each bot turn. On mobile, all chips shown in a horizontal scroll row.",
  configurable: true,
  mobileAdaptive: true,
});
componentRegistry.register("InlineForm", {
  name: "Inline Form",
  description: "Multi-field data collection form rendered inline in the chat. On mobile, displays 2 fields per step.",
  configurable: true,
  mobileAdaptive: true,
});
componentRegistry.register("AssistantImageCarousel", {
  name: "Image Carousel",
  description: "Horizontal image browser displayed when the bot sends property or room images.",
  configurable: false,
  mobileAdaptive: false,
});
componentRegistry.register("WelcomeCta", {
  name: "Welcome CTA",
  description: "Book a Stay button shown under the opening welcome message for pre-booking phase.",
  configurable: true,
  mobileAdaptive: false,
});
componentRegistry.register("MessageBubble", {
  name: "Message Bubble",
  description: "Chat message container for user and assistant turns with markdown rendering.",
  configurable: false,
  mobileAdaptive: false,
});
componentRegistry.register("TypingIndicator", {
  name: "Typing Indicator",
  description: "Animated dots displayed while the bot is generating a response.",
  configurable: false,
  mobileAdaptive: false,
});
componentRegistry.register("LocationPicker", {
  name: "Location Picker",
  description: "Dropdown shown for multi-property hotels to let the guest choose a location.",
  configurable: false,
  mobileAdaptive: false,
});
