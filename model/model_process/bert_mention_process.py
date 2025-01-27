# encoding: utf-8


import torch
import numpy as np
import torch.nn.functional as F
import torch.nn as nn
from sklearn import metrics
from transformers import AdamW
from transformers.optimization import get_linear_schedule_with_warmup

from util.model_util import ModelUtil
from util.log_util import LogUtil
from model.model_metric.bert_mention_metric import BERTMentionMetric

class BERTMentionProcess(object):
    """
    训练、验证、测试BERT Mention分类模型
    """
    def __init__(self, model_config):
        self.model_config = model_config
        self.args = self.model_config.args
        self.model_util = ModelUtil()
        self.model_metric = BERTMentionMetric()

    def train(self, model, train_loader, dev_loader):
        """
        训练模型
        :param model: 模型
        :param train_loader: 训练数据
        :param dev_loader: 验证数据
        :return:
        """
        model.train()
        # Prepare optimizer and schedule (linear warmup and decay)
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
             "weight_decay": self.args.weight_decay},
            {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
             "weight_decay": 0.0},
        ]
        t_total = len(train_loader) * self.model_config.num_epochs
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.args.learning_rate, eps=self.args.adam_epsilon)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=int(t_total * self.args.warmup_proportion),
                                                    num_training_steps=t_total)

        # 多GPU训练
        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model)

        # 记录进行到多少batch
        total_batch = 0
        dev_best_acc = 0
        # 记录上次验证集loss下降的batch数
        last_improve = 0
        # 记录是否很久没有效果提升
        no_improve_flag = False

        LogUtil.logger.info("Batch Num: {0}".format(len(train_loader)))
        for epoch in range(self.model_config.num_epochs):
            LogUtil.logger.info("Epoch [{}/{}]".format(epoch + 1, self.model_config.num_epochs))
            for i, batch_data in enumerate(train_loader):
                # 将数据加载到gpu
                batch_data = tuple(ele.to(self.model_config.device) for ele in batch_data)
                input_ids, input_mask, type_ids, mention_begins, mention_ends, label_ids = batch_data
                outputs = model((input_ids, input_mask, type_ids, mention_begins, mention_ends))
                model.zero_grad()
                loss = F.cross_entropy(outputs, label_ids)
                loss.backward()
                # 对norm大于1的梯度进行修剪
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()

                total_batch += 1
                # 每多少轮输出在训练集和验证集上的效果
                if total_batch % self.model_config.per_eval_batch_step == 0:
                    # torch.max返回一个元组（最大值列表, 最大值对应的index列表）
                    pred_ids = torch.argmax(outputs.data, dim=1)
                    label_id_data = label_ids.data
                    total = label_id_data.size(0)
                    correct = (pred_ids == label_id_data).sum().item()
                    train_acc = correct / total
                    dev_loss, dev_acc = self.evaluate(model, dev_loader)
                    if dev_acc > dev_best_acc:
                        dev_best_acc = dev_acc
                        torch.save(model.state_dict(), self.model_config.model_save_path)
                        improve = "*"
                        last_improve = total_batch
                    else:
                        improve = ""
                    msg = "Iter: {0:>6},  Train Loss: {1:>5.2},  Train Acc: {2:>6.2%}, " \
                          "Dev Loss: {3:>5.2},  Dev Acc: {4:>6.2%} {5}"
                    LogUtil.logger.info(msg.format(total_batch, loss.item(), train_acc,
                                                   dev_loss, dev_acc, improve))
                    model.train()
                
                if total_batch - last_improve > self.model_config.require_improvement:
                    # 验证集loss超过require_improvement没下降，结束训练
                    LogUtil.logger.info("No optimization for a long time, auto-stopping...")
                    no_improve_flag = True
                    break
            if no_improve_flag:
                break

    def evaluate(self, model, data_loader):
        """
        验证模型
        :param model:
        :param data_loader:
        :param is_test: 是否为测试集
        :return:
        """
        model.eval()
        loss_total = 0
        predict_all = np.array([], dtype=int)
        labels_all = np.array([], dtype=int)

        with torch.no_grad():
            for i, batch_data in enumerate(data_loader):
                # 将数据加载到gpu
                batch_data = tuple(ele.to(self.model_config.device) for ele in batch_data)
                input_ids, input_mask, type_ids, mention_begins, mention_ends, label_ids = batch_data
                outputs = model((input_ids, input_mask, type_ids, mention_begins, mention_ends))
                loss = F.cross_entropy(outputs, label_ids)
                loss_total += loss
                pred_ids = torch.max(outputs.data, axis=1)[1].cpu().numpy()
                predict_all = np.append(predict_all, pred_ids)
                labels_all = np.append(labels_all, label_ids.data.cpu().numpy())

        dev_loss = loss_total / len(data_loader)
        dev_acc = metrics.accuracy_score(labels_all, predict_all)

        return dev_loss, dev_acc

    def test(self, model, test_loader):
        """
        测试模型
        :param model:
        :param test_loader:
        :return:
        """
        # 加载模型
        self.model_util.load_model(model, self.model_config.model_save_path, self.model_config.device)

        model.eval()
        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model)

        test_loss, test_acc = self.evaluate(model, test_loader)
        LogUtil.logger.info("Test Loss: {0}, Test Acc: {1}".format(test_loss, test_acc))

    def predict(self, model, predict_loader):
        """
        预测模型
        :param model:
        :param predict_loader:
        :return:
        """
        # 加载模型
        self.model_util.load_model(model, self.model_config.model_save_path, self.model_config.device)

        model.eval()
        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model)

        all_predict_id_list = []
        all_score_list = []
        with torch.no_grad():
            for i, batch_data in enumerate(predict_loader):
                # 将数据加载到gpu
                batch_data = tuple(ele.to(self.model_config.device) for ele in batch_data)
                input_ids, input_mask, type_ids, mention_begins, mention_ends, label_ids = batch_data
                outputs = model((input_ids, input_mask, type_ids, mention_begins, mention_ends))
                scores, pred_ids = torch.max(outputs.data, axis=1)
                scores = scores.cpu().numpy().tolist()
                pred_ids = pred_ids.cpu().numpy().tolist()
                all_predict_id_list.extend(pred_ids)
                all_score_list.extend(scores)

        all_pred_label_list = [self.model_config.id_label_dict[pred_id] for pred_id in all_predict_id_list]

        return all_pred_label_list, all_score_list

    def test_by_connect_model(self, model, test_loader, pred_sent_entity_dict, all_sent_label_dict):
        """
        测试预测结果
        :param model:
        :param test_loader:
        :param all_sent_label_dict:
        :return:
        """
        # 加载模型
        self.model_util.load_model(model, self.model_config.model_save_path, self.model_config.device)

        model.eval()
        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model)

        with torch.no_grad():
            for i, batch_data in enumerate(test_loader):
                # 将数据加载到gpu
                batch_data = tuple(ele.to(self.model_config.device) for ele in batch_data)
                input_ids, input_mask, type_ids, mention_begins, mention_ends, sent_indexs = batch_data
                outputs = model((input_ids, input_mask, type_ids, mention_begins, mention_ends))
                scores, pred_ids = torch.max(outputs.data, axis=1)
                self.model_metric.update_eval_result(pred_ids.cpu().numpy().tolist(),
                                                     scores.cpu().numpy().tolist(),
                                                     mention_begins.cpu().numpy().tolist(),
                                                     mention_ends.cpu().numpy().tolist(),
                                                     sent_indexs.cpu().numpy().tolist(),
                                                     pred_sent_entity_dict, self.model_config)
        metric_result_dict = self.model_metric.get_metric_result(all_sent_label_dict)
        LogUtil.logger.info(metric_result_dict)

        return metric_result_dict
