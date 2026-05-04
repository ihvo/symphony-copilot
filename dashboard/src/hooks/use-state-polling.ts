import useSWR from "swr";
import type { SystemState } from "@/lib/types";
import { fetchState } from "@/lib/api";

export function useStatePolling() {
  const { data, error, isLoading, mutate } = useSWR<SystemState>(
    "/api/v1/state",
    fetchState,
    {
      refreshInterval: 10_000,
      revalidateOnFocus: true,
      dedupingInterval: 5_000,
      refreshWhenHidden: false,
    }
  );

  return {
    state: data,
    isLoading,
    isError: !!error,
    isStale: !isLoading && !data && !error,
    refresh: mutate,
  };
}
