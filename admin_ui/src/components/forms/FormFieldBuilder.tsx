import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ArrowDown, ArrowUp, GripVertical, Plus, Trash2 } from "lucide-react";
import type { FieldTypeOption, FieldTypeValue, FormField } from "./types";

interface Props {
  fields: FormField[];
  fieldTypes: FieldTypeOption[];
  onFieldsChange: (fields: FormField[]) => void;
}

/** A stable client-side key, so reordering never remounts the wrong row. */
const newKey = () => `f_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;

const FormFieldBuilder = ({ fields, fieldTypes, onFieldsChange }: Props) => {
  const patch = (index: number, updates: Partial<FormField>) => {
    onFieldsChange(fields.map((f, i) => (i === index ? { ...f, ...updates } : f)));
  };

  const addField = () => {
    onFieldsChange([
      ...fields,
      {
        _key: newKey(),
        label: "New field",
        fieldType: "CUSTOM",
        type: "text",
        required: false,
        placeholder: "",
      },
    ]);
  };

  const removeField = (index: number) => {
    onFieldsChange(fields.filter((_, i) => i !== index));
  };

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= fields.length) return;
    const next = [...fields];
    [next[index], next[target]] = [next[target], next[index]];
    onFieldsChange(next);
  };

  // Picking "Phone number" should also set the input type and validation hints,
  // so an operator never has to know the underlying encoding.
  const changeType = (index: number, value: FieldTypeValue) => {
    const meta = fieldTypes.find((t) => t.value === value);
    const updates: Partial<FormField> = {
      fieldType: value,
      type: meta?.type ?? "text",
      numberValidationType: undefined,
      textValidationType: undefined,
    };
    if (meta?.type === "number") updates.numberValidationType = "PHONE";
    if (meta?.type === "email") updates.textValidationType = "EMAIL";
    if (meta?.type === "text") updates.textValidationType = "PLAIN";
    patch(index, updates);
  };

  return (
    <div className="space-y-3">
      {fields.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
          No fields yet. Add at least one so guests have something to fill in.
        </div>
      ) : null}

      {fields.map((field, index) => (
        <div
          key={field._key ?? `${field.fieldType}-${index}`}
          className="rounded-lg border bg-card p-3"
        >
          <div className="flex items-start gap-2">
            <div className="flex flex-col items-center pt-2 text-muted-foreground">
              <GripVertical className="h-4 w-4" />
              <span className="mt-1 text-[10px] font-mono">{index + 1}</span>
            </div>

            <div className="grid flex-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label className="text-xs">Label shown to the guest</Label>
                <Input
                  value={field.label}
                  onChange={(e) => patch(index, { label: e.target.value })}
                  placeholder="e.g. Full name"
                />
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs">Field type</Label>
                <Select
                  value={field.fieldType}
                  onValueChange={(v) => changeType(index, v as FieldTypeValue)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {fieldTypes.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        {t.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5 sm:col-span-2">
                <Label className="text-xs">Placeholder</Label>
                <Input
                  value={field.placeholder ?? ""}
                  onChange={(e) => patch(index, { placeholder: e.target.value })}
                  placeholder="Hint text inside the empty box"
                />
              </div>
            </div>

            <div className="flex flex-col items-end gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-destructive"
                onClick={() => removeField(index)}
                title="Remove field"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                disabled={index === 0}
                onClick={() => move(index, -1)}
                title="Move up"
              >
                <ArrowUp className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                disabled={index === fields.length - 1}
                onClick={() => move(index, 1)}
                title="Move down"
              >
                <ArrowDown className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>

          <div className="mt-2 flex items-center gap-2 border-t pt-2">
            <Switch
              checked={Boolean(field.required)}
              onCheckedChange={(v) => patch(index, { required: v })}
              id={`req-${index}`}
            />
            <Label htmlFor={`req-${index}`} className="text-xs font-normal">
              Required — the guest cannot submit without it
            </Label>
          </div>
        </div>
      ))}

      <Button variant="outline" size="sm" onClick={addField} className="w-full">
        <Plus className="mr-2 h-4 w-4" /> Add field
      </Button>
    </div>
  );
};

export default FormFieldBuilder;
