import { invoke } from "@tauri-apps/api/core";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { showErrorToast, showSuccessToast } from "@/lib/toast-utils";

export function LocalFeaturesBar() {
  const { t } = useTranslation();
  const [port, setPort] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void invoke<{ port: number; token: string } | null>("get_mcp_config")
        .then((value) => {
          if (active) setPort(value?.port ?? null);
        })
        .catch((error) => console.error("Cannot read local MCP status", error));
    };
    refresh();
    const interval = window.setInterval(refresh, 2000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  async function toggle() {
    setBusy(true);
    try {
      if (port) {
        await invoke("stop_mcp_server");
        setPort(null);
      } else {
        setPort(await invoke<number>("start_mcp_server"));
      }
    } catch (error) {
      showErrorToast(t("localFeatures.mcpError"), {
        description: String(error),
      });
    } finally {
      setBusy(false);
    }
  }

  async function copyConnection() {
    try {
      const config = await invoke<{ port: number; token: string } | null>(
        "get_mcp_config",
      );
      if (!config) throw new Error("MCP configuration unavailable");
      await writeText(
        JSON.stringify(
          {
            mcpServers: {
              donutLocal: {
                url: `http://127.0.0.1:${config.port}/mcp`,
                headers: { Authorization: `Bearer ${config.token}` },
              },
            },
          },
          null,
          2,
        ),
      );
      showSuccessToast(t("localFeatures.copied"));
    } catch (error) {
      showErrorToast(t("localFeatures.mcpError"), {
        description: String(error),
      });
    }
  }

  return (
    <div
      className="flex flex-wrap items-center gap-2 border-b px-3 py-2 text-xs"
      data-testid="local-features-bar"
    >
      <span className="font-medium">{t("localFeatures.title")}</span>
      <span className="flex-1 text-muted-foreground">
        {t("localFeatures.description")}
      </span>
      <Button
        size="sm"
        variant="outline"
        disabled={busy}
        onClick={() => void toggle()}
      >
        {port
          ? t("localFeatures.stopMcp", { port })
          : t("localFeatures.startMcp")}
      </Button>
      {port && (
        <Button
          size="sm"
          variant="outline"
          onClick={() => void copyConnection()}
        >
          {t("localFeatures.copy")}
        </Button>
      )}
    </div>
  );
}
