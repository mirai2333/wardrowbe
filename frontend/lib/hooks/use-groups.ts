'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSession } from 'next-auth/react';

import { api } from '@/lib/api';
import { queryKeys } from '@/lib/hooks/query-keys';
import { useSetTokenIfAvailable } from '@/lib/hooks/use-session-token';
import type { ItemGroup, ItemGroupDetail } from '@/lib/types';

export const groupsQueryKey = ['groups'] as const;

export function useGroups() {
  const { status } = useSession();
  useSetTokenIfAvailable();

  return useQuery({
    queryKey: groupsQueryKey,
    queryFn: () => api.get<ItemGroup[]>('/groups'),
    enabled: status !== 'loading',
  });
}

export function useCreateGroup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (name: string) => api.post<ItemGroupDetail>('/groups', { name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: groupsQueryKey }),
  });
}

export function useRenameGroup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ groupId, name }: { groupId: string; name: string }) =>
      api.patch<ItemGroupDetail>(`/groups/${groupId}`, { name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: groupsQueryKey }),
  });
}

export function useDeleteGroup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (groupId: string) => api.delete<void>(`/groups/${groupId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: groupsQueryKey });
      // Items list pages can be filtered by a group, so their cached pages are stale too.
      queryClient.invalidateQueries({ queryKey: queryKeys.items.all });
    },
  });
}

export function useSetGroupItems() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ groupId, itemIds }: { groupId: string; itemIds: string[] }) =>
      api.put<ItemGroupDetail>(`/groups/${groupId}/items`, { item_ids: itemIds }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: groupsQueryKey });
      queryClient.invalidateQueries({ queryKey: queryKeys.items.all });
    },
  });
}
