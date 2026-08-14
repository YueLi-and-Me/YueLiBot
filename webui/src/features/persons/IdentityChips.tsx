/**
 * 人物身份与群成员关系的标签组渲染，由人物列表页与详情页共用。
 *
 * 身份呈现规则：QQ 身份拆分为「QQ昵称」和「QQ号」两片，避免昵称与账号混在
 * 同一片中难以辨认；其他平台合并为「平台名 / 昵称 · 外部标识」一片，保留原始
 * 字段不做翻译。群成员关系按群号单独成片，展示该人在群内的名片。
 */
import { Fragment } from 'react'

import { Chip } from '@/components/ui'
import type { GroupMembership, PersonIdentity } from '../../../../electron/shared/ipc.ts'

interface IdentityChipsProps {
  /** 人物已绑定的平台身份。 */
  identities: PersonIdentity[]
  /** 人物所属的群成员关系。 */
  groupMemberships: GroupMembership[]
}

/**
 * 渲染身份与群名片标签组。
 *
 * @param props.identities 平台身份列表；为空时展示「尚未绑定平台身份」占位片。
 * @param props.groupMemberships 群成员关系列表；群名片为空时展示「未设置群名片」。
 * @returns 自动换行的标签组容器。
 */
export function IdentityChips({ identities, groupMemberships }: IdentityChipsProps) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {identities.map((identity) =>
        identity.platform === 'qq' ? (
          <Fragment key={`${identity.platform}-${identity.externalId}`}>
            <Chip label="QQ昵称" value={identity.displayName} />
            <Chip label="QQ号" value={identity.externalId} />
          </Fragment>
        ) : (
          <Chip
            key={`${identity.platform}-${identity.externalId}`}
            label={identity.platform.toUpperCase()}
            value={`${identity.displayName} · ${identity.externalId}`}
          />
        ),
      )}
      {identities.length === 0 ? <Chip label="身份" value="尚未绑定平台身份" /> : null}
      {groupMemberships.map((membership) => (
        <Chip
          key={membership.streamId}
          label={`QQ群 ${membership.groupExternalId}`}
          value={membership.groupCard || '未设置群名片'}
        />
      ))}
    </div>
  )
}
