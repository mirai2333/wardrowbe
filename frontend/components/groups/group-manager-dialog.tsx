'use client';

import { useEffect, useState } from 'react';
import Image from 'next/image';
import { useTranslations } from 'next-intl';
import { Layers, Pencil, Plus, Trash2 } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ItemPicker } from '@/components/shared/item-picker';
import { api, getErrorMessage } from '@/lib/api';
import {
  useCreateGroup,
  useDeleteGroup,
  useGroups,
  useRenameGroup,
  useSetGroupItems,
} from '@/lib/hooks/use-groups';
import type { ItemGroup, ItemGroupDetail } from '@/lib/types';

interface GroupManagerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function GroupManagerDialog({ open, onOpenChange }: GroupManagerDialogProps) {
  const t = useTranslations('wardrobe');
  const { data: groups, isLoading } = useGroups();
  const createGroup = useCreateGroup();
  const renameGroup = useRenameGroup();
  const deleteGroup = useDeleteGroup();
  const setGroupItems = useSetGroupItems();

  const [editing, setEditing] = useState<ItemGroup | null>(null);
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const [name, setName] = useState('');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    if (!open) {
      setIsEditorOpen(false);
      setEditing(null);
      setName('');
      setSelectedIds(new Set());
    }
  }, [open]);

  const closeEditor = () => {
    setIsEditorOpen(false);
    setEditing(null);
    setName('');
    setSelectedIds(new Set());
  };

  const openCreate = () => {
    setEditing(null);
    setName('');
    setSelectedIds(new Set());
    setIsEditorOpen(true);
  };

  const openEdit = async (group: ItemGroup) => {
    setEditing(group);
    setName(group.name);
    setSelectedIds(new Set());
    setIsEditorOpen(true);
    try {
      // The list response only carries a preview; the detail holds the full membership.
      const detail = await api.get<ItemGroupDetail>(`/groups/${group.id}`);
      setSelectedIds(new Set(detail.item_ids));
    } catch {
      toast.error(t('groups.loadMembersFailed'));
    }
  };

  const toggleItem = (item: { id: string }) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(item.id)) next.delete(item.id);
      else next.add(item.id);
      return next;
    });
  };

  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;

    setIsSaving(true);
    try {
      const group = editing
        ? await renameGroup.mutateAsync({ groupId: editing.id, name: trimmed })
        : await createGroup.mutateAsync(trimmed);
      await setGroupItems.mutateAsync({
        groupId: group.id,
        itemIds: Array.from(selectedIds),
      });
      toast.success(t('groups.saved'));
      closeEditor();
    } catch (error) {
      toast.error(getErrorMessage(error, t('groups.saveFailed')));
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (group: ItemGroup) => {
    if (!window.confirm(t('groups.deleteConfirm', { name: group.name }))) return;

    try {
      await deleteGroup.mutateAsync(group.id);
      toast.success(t('groups.deleted'));
      if (editing?.id === group.id) closeEditor();
    } catch (error) {
      toast.error(getErrorMessage(error, t('groups.deleteFailed')));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[88vh] flex flex-col p-6">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Layers className="h-5 w-5 text-primary" />
            {isEditorOpen
              ? editing
                ? t('groups.editTitle')
                : t('groups.createTitle')
              : t('groups.title')}
          </DialogTitle>
          <DialogDescription>
            {isEditorOpen ? t('groups.editorDescription') : t('groups.description')}
          </DialogDescription>
        </DialogHeader>

        {isEditorOpen ? (
          <>
            <div className="pt-1">
              <Input
                value={name}
                maxLength={50}
                placeholder={t('groups.namePlaceholder')}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
            </div>
            <div className="flex-1 overflow-hidden min-h-0 pt-2">
              <ItemPicker
                selectedIds={selectedIds}
                onToggle={toggleItem}
                hideNeedsWash={true}
                heightClass="h-[300px]"
              />
            </div>
            <div className="flex justify-between items-center gap-2 pt-3 border-t mt-2">
              <span className="text-xs text-muted-foreground">
                {t('groups.selectedCount', { count: selectedIds.size })}
              </span>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={closeEditor} disabled={isSaving}>
                  {t('groups.cancel')}
                </Button>
                <Button onClick={handleSave} disabled={isSaving || !name.trim()}>
                  {t('groups.save')}
                </Button>
              </div>
            </div>
          </>
        ) : (
          <div className="flex-1 overflow-y-auto space-y-3">
            <Button variant="outline" className="w-full" onClick={openCreate}>
              <Plus className="mr-2 h-4 w-4" />
              {t('groups.create')}
            </Button>

            {isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
              </div>
            ) : groups && groups.length > 0 ? (
              <div className="space-y-2">
                {groups.map((group) => (
                  <div
                    key={group.id}
                    className="flex items-center gap-3 rounded-lg border p-3"
                  >
                    <div className="flex -space-x-2">
                      {group.preview_items.slice(0, 3).map((item) => (
                        <div
                          key={item.id}
                          className="relative h-10 w-10 overflow-hidden rounded-full border-2 border-background bg-muted"
                        >
                          {item.thumbnail_url ? (
                            <Image
                              src={item.thumbnail_url}
                              alt={item.name ?? ''}
                              fill
                              className="object-cover"
                              sizes="40px"
                            />
                          ) : null}
                        </div>
                      ))}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{group.name}</p>
                      <p className="text-xs text-muted-foreground">
                        {t('groups.itemCount', { count: group.item_count })}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => openEdit(group)}
                      title={t('groups.edit')}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => handleDelete(group)}
                      title={t('groups.delete')}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t('groups.empty')}</p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
