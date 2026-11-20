export const JSON_TYPES = [
  { value: "string", label: "文本" },
  { value: "number", label: "数字" },
  { value: "boolean", label: "布尔值" },
  { value: "object", label: "对象" },
  { value: "array", label: "列表" },
  { value: "null", label: "空值" },
];

export const FORBIDDEN_KEYS = new Set([
  "__proto__",
  "prototype",
  "constructor",
]);

export function cloneValue(value) {
  if (value === undefined) return undefined;
  // Values are guaranteed to be strict JSON by the backend. JSON cloning also
  // unwraps Vue reactive proxies, which structuredClone intentionally rejects.
  return JSON.parse(JSON.stringify(value));
}

function pointer(root, reference) {
  if (reference === "#") return root;
  if (reference?.startsWith("#/"))
    return decodeURIComponent(reference.slice(2))
      .split("/")
      .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"))
      .reduce((value, key) => value?.[key], root);
  if (!reference?.startsWith("#")) return null;
  const anchor = decodeURIComponent(reference.slice(1));
  const pending = [root];
  while (pending.length) {
    const value = pending.pop();
    if (!value || typeof value !== "object") continue;
    if (value.$anchor === anchor || value.$dynamicAnchor === anchor)
      return value;
    pending.push(...Object.values(value));
  }
  return null;
}

function mergeSchemas(parts) {
  return parts.reduce((result, part) => {
    const next = { ...result, ...part };
    if (result.properties || part.properties)
      next.properties = {
        ...(result.properties || {}),
        ...(part.properties || {}),
      };
    if (result.required || part.required)
      next.required = [
        ...new Set([...(result.required || []), ...(part.required || [])]),
      ];
    return next;
  }, {});
}

export function resolveSchema(schema = {}, root = schema, trail = new Set()) {
  let value = schema && typeof schema === "object" ? schema : {};
  if (value.$ref && !trail.has(value.$ref)) {
    const target = pointer(root, value.$ref);
    if (target)
      value = {
        ...resolveSchema(target, root, new Set(trail).add(value.$ref)),
        ...Object.fromEntries(
          Object.entries(value).filter(([key]) => key !== "$ref"),
        ),
      };
  }
  if (Array.isArray(value.allOf))
    value = mergeSchemas([
      ...value.allOf.map((item) => resolveSchema(item, root, trail)),
      Object.fromEntries(
        Object.entries(value).filter(([key]) => key !== "allOf"),
      ),
    ]);
  const variants = value.anyOf;
  if (Array.isArray(variants)) {
    const nonNull = variants.filter((item) => item?.type !== "null");
    if (nonNull.length === 1 && nonNull.length !== variants.length)
      value = {
        ...resolveSchema(nonNull[0], root, trail),
        ...Object.fromEntries(
          Object.entries(value).filter(
            ([key]) => !["anyOf", "type"].includes(key),
          ),
        ),
        nullable: true,
      };
  }
  return value;
}

export function valueType(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "object") return "object";
  if (typeof value === "number")
    return Number.isInteger(value) ? "integer" : "number";
  if (["string", "boolean"].includes(typeof value)) return typeof value;
  return "string";
}

export function inferredSchema(value) {
  const type = valueType(value);
  if (type === "object") return { type: "object", additionalProperties: {} };
  if (type === "array") return { type: "array", items: {} };
  return { type };
}

export function effectiveSchema(schema, value, root = schema) {
  const resolved = resolveSchema(schema, root);
  return Object.keys(resolved).length ? resolved : inferredSchema(value);
}

export function variantsFor(schema, root = schema) {
  const resolved = resolveSchema(schema, root);
  const variants =
    resolved.oneOf ||
    (Array.isArray(resolved.anyOf) && resolved.anyOf.length > 1
      ? resolved.anyOf
      : null);
  return (variants || []).map((item, index) => {
    const definition = resolveSchema(item, root);
    return {
      schema: definition,
      index,
      label:
        definition.title ||
        definition.const ||
        definition.type ||
        `选项 ${index + 1}`,
    };
  });
}

export function finiteChoicesFor(schema, root = schema) {
  const variants = variantsFor(schema, root);
  if (!variants.length) return [];
  const choices = [];
  for (const variant of variants) {
    const definition = resolveSchema(variant.schema, root);
    if (Object.hasOwn(definition, "const")) {
      choices.push({ value: definition.const, label: variant.label });
      continue;
    }
    if (Array.isArray(definition.enum) && definition.enum.length) {
      definition.enum.forEach((value) =>
        choices.push({
          value,
          label:
            definition.enum.length === 1 && definition.title
              ? definition.title
              : String(value),
        }),
      );
      continue;
    }
    if (definition.type === "boolean") {
      choices.push(
        { value: false, label: "关闭" },
        { value: true, label: "开启" },
      );
      continue;
    }
    return [];
  }
  const unique = choices.filter(
    (choice, index) =>
      choices.findIndex((candidate) =>
        Object.is(candidate.value, choice.value),
      ) === index,
  );
  return unique.length <= 6 ? unique : [];
}

export function schemaType(schema, value, root = schema) {
  const resolved = effectiveSchema(schema, value, root);
  if (resolved.const !== undefined) return valueType(resolved.const);
  if (Array.isArray(resolved.type))
    return resolved.type.find((item) => item !== "null") || "null";
  return resolved.type || valueType(value);
}

export function defaultValue(schema = {}, root = schema) {
  const resolved = resolveSchema(schema, root);
  if (Object.hasOwn(resolved, "default")) return cloneValue(resolved.default);
  if (Object.hasOwn(resolved, "const")) return cloneValue(resolved.const);
  const variants = variantsFor(resolved, root);
  if (variants.length) return defaultValue(variants[0].schema, root);
  const type = schemaType(resolved, undefined, root);
  if (type === "object") {
    const result = {};
    for (const [key, field] of Object.entries(resolved.properties || {})) {
      const definition = resolveSchema(field, root);
      if (Object.hasOwn(definition, "default"))
        result[key] = cloneValue(definition.default);
    }
    return result;
  }
  if (type === "array") return [];
  if (type === "boolean") return false;
  if (["integer", "number"].includes(type)) return resolved.minimum ?? 0;
  if (type === "null") return null;
  return "";
}

export function typeLabel(schema, value, root = schema) {
  const type = schemaType(schema, value, root);
  return (
    {
      string: "文本",
      integer: "整数",
      number: "数字",
      boolean: "布尔值",
      object: "对象",
      array: "列表",
      null: "空值",
    }[type] || type
  );
}

export function isComplex(schema, value, root = schema) {
  return ["object", "array"].includes(schemaType(schema, value, root));
}

export function summary(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === "object") {
    const entries = Object.entries(value || {});
    const identity = ["name", "title", "label", "id", "key"]
      .map((key) => entries.find(([candidate]) => candidate === key))
      .find((entry) => ["string", "number"].includes(typeof entry?.[1]))?.[1];
    const count = `${entries.length} 个字段`;
    return identity === undefined || identity === ""
      ? count
      : `${String(identity)} · ${count}`;
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  const text = String(value ?? "");
  return text.length > 54 ? `${text.slice(0, 51)}…` : text || "空值";
}

export function schemaForType(type) {
  if (type === "object") return { type: "object", additionalProperties: {} };
  if (type === "array") return { type: "array", items: {} };
  return { type };
}
