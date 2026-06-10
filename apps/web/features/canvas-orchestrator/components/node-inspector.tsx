"use client";

// Right-rail editor for the selected line node (PL-157 phase 2). Emits a
// partial patch on every field change; the parent commits it to the canvas
// (and thus to undo history).

import { Input } from "@multica/ui/components/ui/input";
import { Label } from "@multica/ui/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@multica/ui/components/ui/native-select";
import { Textarea } from "@multica/ui/components/ui/textarea";

import type { LineExecutor, LineMode, LineNode } from "../lib/line-ir";

interface NodeInspectorProps {
  node: LineNode | null;
  onChange: (patch: Partial<LineNode>) => void;
}

export function NodeInspector({ node, onChange }: NodeInspectorProps) {
  if (!node) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-center text-xs text-muted-foreground">
        选择一个节点以编辑其指令、执行器与归属路径。
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="text-xs font-medium text-muted-foreground">
        节点 <span className="font-mono text-foreground">{node.id}</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="flex flex-col gap-1">
          <Label className="text-xs">执行器</Label>
          <NativeSelect
            className="w-full"
            value={node.executor}
            onChange={(e) => onChange({ executor: e.target.value as LineExecutor })}
          >
            <NativeSelectOption value="claude">claude</NativeSelectOption>
            <NativeSelectOption value="codex">codex</NativeSelectOption>
          </NativeSelect>
        </div>
        <div className="flex flex-col gap-1">
          <Label className="text-xs">模式</Label>
          <NativeSelect
            className="w-full"
            value={node.mode}
            onChange={(e) => {
              const mode = e.target.value as LineMode;
              // audit mode implies an audit node/role; write/read implies dev.
              onChange(
                mode === "audit"
                  ? { mode, kind: "audit", role: "audit" }
                  : { mode, kind: "dev", role: "dev" },
              );
            }}
          >
            <NativeSelectOption value="write">write</NativeSelectOption>
            <NativeSelectOption value="read">read</NativeSelectOption>
            <NativeSelectOption value="audit">audit</NativeSelectOption>
          </NativeSelect>
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <Label className="text-xs">指令</Label>
        <Textarea
          rows={5}
          value={node.instruction}
          placeholder="这个节点要做什么…"
          onChange={(e) => onChange({ instruction: e.target.value })}
          className="resize-none text-xs"
        />
      </div>

      {node.mode === "write" && (
        <div className="flex flex-col gap-1">
          <Label className="text-xs">归属路径（逗号分隔）</Label>
          <Input
            value={node.ownedPaths.join(", ")}
            placeholder="src/foo.ts, src/bar.ts"
            onChange={(e) =>
              onChange({
                ownedPaths: e.target.value
                  .split(",")
                  .map((p) => p.trim())
                  .filter(Boolean),
              })
            }
            className="text-xs"
          />
          <p className="text-[10px] text-muted-foreground">
            write 节点必填；同一波次内路径不得重叠。
          </p>
        </div>
      )}
    </div>
  );
}
