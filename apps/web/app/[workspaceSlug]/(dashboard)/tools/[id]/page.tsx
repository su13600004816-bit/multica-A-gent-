"use client";

import { use } from "react";
import { ToolDetailPage } from "@multica/views/tools";

export default function ToolDetailRoute({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <ToolDetailPage name={decodeURIComponent(id)} />;
}
