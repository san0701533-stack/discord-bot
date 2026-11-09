const { Client, GatewayIntentBits, EmbedBuilder, ActionRowBuilder, ButtonBuilder, ButtonStyle, ChannelType, PermissionFlagsBits } = require('discord.js');

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
    ],
});

client.once('ready', () => {
    console.log(`${client.user.tag} としてログイン完了！`);
});

// 1. 自動返信機能
client.on('messageCreate', async (message) => {
    if (message.author.bot) return;

    if (message.content === 'こんにちは') {
        await message.reply('こんにちは！何かお手伝いできることはありますか？');
    }

    if (message.content === '!ticket') {
        const embed = new EmbedBuilder()
            .setTitle('🎫 お問い合わせチケット')
            .setDescription('下のボタンを押すと専用のお問い合わせチャンネルを作成します。')
            .setColor(0x00AE86);

        const row = new ActionRowBuilder().addComponents(
            new ButtonBuilder()
                .setCustomId('create_ticket')
                .setLabel('チケットを作成する')
                .setStyle(ButtonStyle.Primary)
                .setEmoji('📩')
        );

        await message.channel.send({ embeds: [embed], components: [row] });
    }
});

// 2. チケットシステム（ボタン処理）
client.on('interactionCreate', async (interaction) => {
    if (!interaction.isButton()) return;

    if (interaction.customId === 'create_ticket') {
        const guild = interaction.guild;
        const userName = interaction.user.username;

        const existingChannel = guild.channels.cache.find(c => c.name === `ticket-${userName.toLowerCase()}`);
        if (existingChannel) {
            return interaction.reply({ content: 'すでにチケットチャンネルが存在します！', ephemeral: true });
        }

        try {
            const ticketChannel = await guild.channels.create({
                name: `ticket-${userName}`,
                type: ChannelType.GuildText,
                permissionOverwrites: [
                    {
                        id: guild.id,
                        deny: [PermissionFlagsBits.ViewChannel],
                    },
                    {
                        id: interaction.user.id,
                        allow: [PermissionFlagsBits.ViewChannel, PermissionFlagsBits.SendMessages],
                    },
                ],
            });

            const closeRow = new ActionRowBuilder().addComponents(
                new ButtonBuilder()
                    .setCustomId('close_ticket')
                    .setLabel('チケットを閉じる')
                    .setStyle(ButtonStyle.Danger)
            );

            await ticketChannel.send({
                content: `${interaction.user}さん、ようこそ！`,
                components: [closeRow]
            });

            await interaction.reply({ content: `チケットを作成しました: ${ticketChannel}`, ephemeral: true });
        } catch (error) {
            console.error(error);
            await interaction.reply({ content: 'チャンネル作成に失敗しました。権限を確認してください。', ephemeral: true });
        }
    }

    if (interaction.customId === 'close_ticket') {
        await interaction.reply('5秒後にチャンネルを削除します...');
        setTimeout(() => {
            interaction.channel.delete().catch(() => {});
        }, 5000);
    }
});

client.login(process.env.DISCORD_TOKEN);
