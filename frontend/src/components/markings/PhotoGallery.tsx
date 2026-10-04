import { useCallback, useEffect, useRef, useState } from 'react'
import { formatDateTime } from '../../lib/markings'
import type { MarkingPhoto } from '../../types'
import { Icon } from './Icon'

interface Props {
  photos: MarkingPhoto[]
  /** 图片地址。管理员看隐藏照片时要带口令，由调用方换成 blob 地址 */
  srcOf?: (photo: MarkingPhoto) => string | undefined
  /** 在大图里看过的照片（审核时用来确认每张都看过） */
  viewed?: ReadonlySet<string>
  onView?: (photo: MarkingPhoto) => void
  canDelete?: (photo: MarkingPhoto) => boolean
  onDelete?: (photo: MarkingPhoto) => void
  /** 管理员：隐藏 / 恢复显示 */
  onToggleHidden?: (photo: MarkingPhoto) => void
  emptyText?: string
}

const ROLE_LABEL: Record<MarkingPhoto['role'], string> = {
  author: '提交者拍摄',
  witness: '其他用户补充',
}

/** 照片缩略图 + 大图查看。键盘：← → 切换、Esc 关闭。 */
export function PhotoGallery({
  photos,
  srcOf,
  viewed,
  onView,
  canDelete,
  onDelete,
  onToggleHidden,
  emptyText = '还没有照片',
}: Props) {
  const [open, setOpen] = useState<number | null>(null)
  const src = useCallback((p: MarkingPhoto) => (srcOf ? srcOf(p) : p.url), [srcOf])

  const show = useCallback(
    (i: number) => {
      setOpen(i)
      const photo = photos[i]
      if (photo) onView?.(photo)
    },
    [photos, onView],
  )

  if (photos.length === 0) {
    return <p className="mk-empty-line">{emptyText}</p>
  }

  return (
    <>
      <ul className="mk-gallery" aria-label={`照片 ${photos.length} 张`}>
        {photos.map((p, i) => {
          const url = src(p)
          return (
            <li key={p.id} className={p.status === 'hidden' ? 'is-hidden' : ''}>
              <button
                type="button"
                className="mk-thumb"
                onClick={() => show(i)}
                aria-label={`查看第 ${i + 1} 张照片（${ROLE_LABEL[p.role]}）`}
              >
                {url ? <img src={url} alt="" loading="lazy" decoding="async" /> : <span className="mk-thumb-wait" />}
                <span className={`mk-thumb-role role-${p.role}`}>{p.role === 'author' ? '提交者' : '补充'}</span>
                {viewed?.has(p.id) && (
                  <span className="mk-thumb-seen" title="已查看">
                    <Icon name="check" size={12} />
                  </span>
                )}
                {p.status === 'hidden' && <span className="mk-thumb-hidden">已隐藏</span>}
              </button>
            </li>
          )
        })}
      </ul>
      {open !== null && photos[open] && (
        <Lightbox
          photos={photos}
          index={open}
          src={src}
          onIndex={show}
          onClose={() => setOpen(null)}
          canDelete={canDelete}
          onDelete={(p) => {
            setOpen(null)
            onDelete?.(p)
          }}
          onToggleHidden={onToggleHidden}
        />
      )}
    </>
  )
}

function Lightbox({
  photos,
  index,
  src,
  onIndex,
  onClose,
  canDelete,
  onDelete,
  onToggleHidden,
}: {
  photos: MarkingPhoto[]
  index: number
  src: (p: MarkingPhoto) => string | undefined
  onIndex: (i: number) => void
  onClose: () => void
  canDelete?: (p: MarkingPhoto) => boolean
  onDelete?: (p: MarkingPhoto) => void
  onToggleHidden?: (p: MarkingPhoto) => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  const photo = photos[index]
  const n = photos.length

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    return () => previous?.focus?.()
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'ArrowRight' && n > 1) {
        onIndex((index + 1) % n)
      } else if (e.key === 'ArrowLeft' && n > 1) {
        onIndex((index - 1 + n) % n)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, n, onClose, onIndex])

  const url = src(photo)
  return (
    <div
      className="mk-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={`照片 ${index + 1} / ${n}`}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <figure>
        {url ? <img src={url} alt={`标注照片 ${index + 1}`} /> : <div className="mk-thumb-wait large" />}
        <figcaption>
          <span>
            {index + 1} / {n} · {ROLE_LABEL[photo.role]} · {formatDateTime(photo.created_at)} ·{' '}
            {photo.width}×{photo.height}
          </span>
          <span className="mk-lightbox-actions">
            {onToggleHidden && (
              <button type="button" onClick={() => onToggleHidden(photo)}>
                <Icon name={photo.status === 'hidden' ? 'eye' : 'eye-off'} size={14} />
                {photo.status === 'hidden' ? '恢复显示' : '隐藏'}
              </button>
            )}
            {canDelete?.(photo) && onDelete && (
              <button type="button" className="danger" onClick={() => onDelete(photo)}>
                <Icon name="trash" size={14} />
                删除
              </button>
            )}
          </span>
        </figcaption>
      </figure>
      {n > 1 && (
        <>
          <button
            type="button"
            className="mk-lightbox-nav prev"
            aria-label="上一张"
            onClick={() => onIndex((index - 1 + n) % n)}
          >
            <Icon name="back" size={22} />
          </button>
          <button
            type="button"
            className="mk-lightbox-nav next"
            aria-label="下一张"
            onClick={() => onIndex((index + 1) % n)}
          >
            <Icon name="chevron" size={22} />
          </button>
        </>
      )}
      <button ref={closeRef} type="button" className="mk-lightbox-close" aria-label="关闭" onClick={onClose}>
        <Icon name="close" size={20} />
      </button>
    </div>
  )
}
