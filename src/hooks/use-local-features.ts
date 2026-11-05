import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";

export interface LocalFeatures {
  enabled: boolean;
  browserAutomation: boolean;
  fingerprintConfiguration: boolean;
  extensionManagement: boolean;
  localMcp: boolean;
  unlimitedLocalRequests: boolean;
}

const NONE: LocalFeatures = {
  enabled: false,
  browserAutomation: false,
  fingerprintConfiguration: false,
  extensionManagement: false,
  localMcp: false,
  unlimitedLocalRequests: false,
};

export function useLocalFeatures(): LocalFeatures {
  const [features, setFeatures] = useState(NONE);
  useEffect(() => {
    let active = true;
    void invoke<LocalFeatures>("get_local_features")
      .then((value) => {
        if (active) setFeatures(value);
      })
      .catch((error) =>
        console.error("Cannot read local build capabilities", error),
      );
    return () => {
      active = false;
    };
  }, []);
  return features;
}
